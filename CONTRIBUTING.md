# Contributing

Thank you for contributing to the OpsRabbit Azure deployment package.

## Before opening a pull request

- Do not commit credentials, Terraform state, plan files, customer identifiers,
  private image locations, or real cloud resource IDs.
- Keep public networking backward compatible unless the change includes a
  documented migration path.
- Update the README and example variables when deployment behavior changes.
- Add or update mocked Terraform plans for new behavior and failure cases.
- Keep provider versions and the dependency lock file aligned.

Run:

```bash
terraform init -backend=false -input=false -lockfile=readonly
terraform fmt -check -recursive
terraform validate
terraform test -no-color
tflint --recursive
```

Pull requests should explain the customer-visible change, security impact,
migration considerations, and verification performed.

Contributions submitted for inclusion are licensed under Apache License 2.0.
