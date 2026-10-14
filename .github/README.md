# CI template

`tests.yml.example` contains the test-and-build workflow: core compatibility on Python 3.10,
and the complete retained suite on Python 3.12.
It is deliberately a template, not an active GitHub Actions workflow: the
publication credential has repository access but lacks GitHub's `workflow`
scope, and the initial push containing an active workflow was rejected.

After using credentials authorized to update workflows, enable it with:

```bash
mkdir -p .github/workflows
cp .github/tests.yml.example .github/workflows/tests.yml
git add .github/workflows/tests.yml
git commit -m "Enable test and build workflow"
git push
```

Local tests and package builds do not need this additional GitHub permission.
No automated CI run is claimed by this publication.
