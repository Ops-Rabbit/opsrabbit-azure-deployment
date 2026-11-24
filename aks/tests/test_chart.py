"""Offline contract checks against the manifests Helm actually renders."""
import pathlib
import subprocess
import tempfile
import unittest

import yaml

AKS = pathlib.Path(__file__).resolve().parents[1]


def render(*args, success=True):
    result = subprocess.run(
        ["helm", "template", "opsrabbit", str(AKS / "chart"), "-n", "opsrabbit",
         "-f", str(AKS / "values.example.yaml"), *args],
        capture_output=True, text=True, check=False,
    )
    if success:
        if result.returncode:
            raise AssertionError(result.stderr)
        return [d for d in yaml.safe_load_all(result.stdout) if d]
    if result.returncode == 0:
        raise AssertionError("Invalid configuration was accepted")
    return result.stderr


class ChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()

    def doc(self, kind, suffix="", docs=None):
        matches = [d for d in (docs if docs is not None else self.docs)
                   if d["kind"] == kind and d["metadata"]["name"].endswith(suffix)]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def test_backend_single_writer_and_image_home_preserved(self):
        deployment = self.doc("Deployment", "-backend")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["terminationGracePeriodSeconds"], 135)
        container = pod["containers"][0]
        mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
        self.assertNotIn("/home/opsbot", mounts)  # preserves bundled node_modules symlink
        for subpath in [".opsrabbit", "git", ".agent-browser", ".codex"]:
            self.assertEqual(mounts[f"/home/opsbot/{subpath}"]["subPath"], subpath)
            self.assertIn(f"/persistent/{subpath}", pod["initContainers"][0]["args"][0])
        env = {v["name"]: v for v in container["env"]}
        self.assertEqual(env["OPSRABBIT_WEB_ORIGIN"]["value"], "https://opsrabbit.example.com")
        self.assertEqual(env["OPSRABBIT_NODE_BASE_URL"]["value"], "https://opsrabbit.example.com/api")
        for key in ["OPSRABBIT_NODE_DATABASE_URL", "BETTER_AUTH_SECRET", "OPSRABBIT_NODE_ENCRYPTION_KEY"]:
            self.assertEqual(env[key]["valueFrom"]["secretKeyRef"]["name"], "opsrabbit-runtime")

    def initialize_browser_storage(self, volume, browsers, source_config):
        pod = self.doc("Deployment", "-backend")["spec"]["template"]["spec"]
        init = pod["initContainers"][0]
        script = init["args"][0].replace(
            "/opt/opsrabbit-agent-browser/browsers", str(browsers)
        ).replace(
            "/home/opsbot/.agent-browser/config.json", str(source_config)
        ).replace("/persistent", str(volume))
        subprocess.run([*init["command"], script], check=True, capture_output=True, text=True)

    def test_browser_defaults_survive_empty_persistent_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            browsers = root / "bundled-browsers"
            browsers.mkdir()
            (browsers / "chrome-fixture").write_text("bundled browser")
            config = root / "image-config.json"
            config.write_text('{"executablePath":"/usr/bin/chromium"}')
            volume = root / "persistent"
            self.initialize_browser_storage(volume, browsers, config)
            profile = volume / ".agent-browser"
            self.assertTrue((profile / "browsers").is_symlink())
            self.assertEqual((profile / "browsers" / "chrome-fixture").read_text(), "bundled browser")
            self.assertEqual((profile / "config.json").read_text(), config.read_text())

    def test_browser_initialization_preserves_existing_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            browsers = root / "bundled-browsers"
            browsers.mkdir()
            config = root / "image-config.json"
            config.write_text('{"executablePath":"/usr/bin/chromium"}')
            volume = root / "persistent"
            profile = volume / ".agent-browser"
            (profile / "browsers").mkdir(parents=True)
            (profile / "browsers" / "existing-profile-marker").write_text("preserve")
            (profile / "config.json").write_text('{"executablePath":"/customer/chrome"}')
            self.initialize_browser_storage(volume, browsers, config)
            self.assertFalse((profile / "browsers").is_symlink())
            self.assertEqual((profile / "browsers" / "existing-profile-marker").read_text(), "preserve")
            self.assertEqual((profile / "config.json").read_text(), '{"executablePath":"/customer/chrome"}')

    def test_https_and_proxy_contract(self):
        pod = self.doc("Deployment", "-web")["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(pod["securityContext"]["runAsUser"], 101)
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        env = {e["name"]: e["value"] for e in container["env"]}
        self.assertEqual(env["WEB_TLS_MODE"], "both")
        self.assertEqual(env["WEB_TLS_REDIRECT"], "true")
        self.assertEqual(env["VITE_API_URL"], "/api")
        self.assertEqual(env["WEB_API_UPSTREAM"], "http://opsrabbit-opsrabbit-backend:8384")
        self.assertEqual(container["readinessProbe"]["httpGet"],
                         {"path": "/api/health", "port": "https", "scheme": "HTTPS"})
        self.assertEqual(container["livenessProbe"]["httpGet"]["path"], "/")
        self.assertEqual(container["startupProbe"]["httpGet"]["path"], "/")
        self.assertIn("listen 8443 ssl", pod["initContainers"][0]["command"][2])
        tls = next(v for v in pod["volumes"] if v["name"] == "tls")
        self.assertEqual(tls["secret"]["secretName"], "opsrabbit-tls")

    def test_only_web_exposed_and_backend_restricted(self):
        backend = self.doc("Service", "-backend")
        web = self.doc("Service", "-web")
        self.assertEqual(backend["spec"]["type"], "ClusterIP")
        self.assertEqual(web["spec"]["type"], "LoadBalancer")
        self.assertEqual([p["port"] for p in web["spec"]["ports"]], [80, 443])
        self.assertEqual(web["metadata"]["annotations"]["service.beta.kubernetes.io/azure-load-balancer-internal"], "true")
        policy = self.doc("NetworkPolicy")["spec"]
        self.assertEqual(policy["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"], "web")
        self.assertEqual(policy["ingress"][0]["ports"][0]["port"], 8384)

    def test_public_service_and_source_allowlist(self):
        docs = render("--set", "service.internal=false", "--set", "service.loadBalancerSourceRanges[0]=203.0.113.0/24")
        web = self.doc("Service", "-web", docs)
        self.assertEqual(web["metadata"]["annotations"]["service.beta.kubernetes.io/azure-load-balancer-internal"], "false")
        self.assertEqual(web["spec"]["loadBalancerSourceRanges"], ["203.0.113.0/24"])

    def test_optional_shared_acme_challenges(self):
        docs = render("--set", "tls.acmeChallengeConfigMap=opsrabbit-acme")
        pod = self.doc("Deployment", "-web", docs)["spec"]["template"]["spec"]
        volume = next(v for v in pod["volumes"] if v["name"] == "acme-challenges")
        self.assertEqual(volume["configMap"]["name"], "opsrabbit-acme")
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "acme-challenges")
        self.assertEqual(mount["mountPath"], "/usr/share/nginx/html/.well-known/acme-challenge")
        self.assertTrue(mount["readOnly"])

    def test_retained_storage_and_existing_claim(self):
        pvc = self.doc("PersistentVolumeClaim")
        storage = self.doc("StorageClass")
        self.assertEqual(storage["reclaimPolicy"], "Retain")
        self.assertEqual(storage["volumeBindingMode"], "WaitForFirstConsumer")
        self.assertEqual(pvc["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")
        self.assertEqual(pvc["spec"]["storageClassName"], storage["metadata"]["name"])
        docs = render("--set", "persistence.existingClaim=recovered-data")
        self.assertFalse(any(d["kind"] in ["PersistentVolumeClaim", "StorageClass"] for d in docs))
        pod = self.doc("Deployment", "-backend", docs)["spec"]["template"]["spec"]
        self.assertEqual(pod["volumes"][0]["persistentVolumeClaim"]["claimName"], "recovered-data")

    def test_no_inline_secrets_or_cloud_specific_gcp_resources(self):
        self.assertFalse(any(d["kind"] in ["Secret", "Ingress", "FrontendConfig", "BackendConfig"] for d in self.docs))
        for deployment in [d for d in self.docs if d["kind"] == "Deployment"]:
            for container in deployment["spec"]["template"]["spec"]["containers"]:
                names = [e["name"] for e in container["env"]]
                self.assertEqual(len(names), len(set(names)))

    def test_unsafe_values_rejected(self):
        for value in ["replicaCount.backend=2", "persistence.enabled=false",
                      "applicationOrigin=http://opsrabbit.example.com",
                      "applicationOrigin=https://opsrabbit.example.com/path",
                      "image.backend=registry.example.com/backend:latest",
                      "secrets.existingSecret=", "tls.existingSecret="]:
            with self.subTest(value=value):
                render("--set", value, success=False)


if __name__ == "__main__":
    unittest.main()
