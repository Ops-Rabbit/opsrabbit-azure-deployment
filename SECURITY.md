# Security policy

## Supported version

Security fixes are made on the default `develop` branch. Use the latest
revision when reporting or validating a potential issue.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting from the repository Security page.
Do not open a public issue for a suspected vulnerability.

Include:

- the affected Terraform revision
- the selected public or private network mode
- a concise description of the impact
- reproducible steps using placeholders rather than real credentials or
  customer identifiers
- any suggested mitigation

Never include Terraform state, cloud credentials, access tokens, private image
references, customer resource identifiers, or production logs containing
sensitive data.

## Deployment responsibility

This repository provisions customer-owned cloud resources. Deployment owners
remain responsible for identity permissions, network policy, remote-state
protection, secret rotation, logging, backup policy, regional resilience, and
their organization's security requirements.
