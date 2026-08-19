# ACR actions

Shared steps for Azure Container Registry, usable from public and private repos in
the org. They take the registry, repository and tag as inputs and hold no
environment-specific values, so callers pass their own through their own secrets and variables.

## mark-tag-immutable

Locks image tags against overwrite and deletion, and verifies the lock stuck. The
registry can rewrite tag metadata for a few seconds after a push and silently drop a
lock applied in that window, so the step settles first, reads the attribute back,
retries, and fails the job when the lock does not hold.

```yaml
- uses: iomete/.github/actions/acr/mark-tag-immutable@main
  with:
    acr_name: ${{ secrets.ACR_NAME }}
    repository: ${{ vars.ACR_NAMESPACE }}/my-image
    tag: 1.2.3
```

`repository` and `tag` each accept a space-separated list, so several images or tags
can be locked in one step. Requires an authenticated `az` session, for example from
`azure/login`.
