## Summary

Describe the customer-visible change.

## Security and migration

- [ ] No credentials, state, customer identifiers, or private image locations
      are included.
- [ ] Public/private networking effects were evaluated.
- [ ] State-address or replacement risks are documented.
- [ ] Documentation and examples were updated where needed.

## Verification

- [ ] `terraform fmt -check -recursive`
- [ ] `terraform validate`
- [ ] `terraform test -no-color`
- [ ] `tflint --recursive`
