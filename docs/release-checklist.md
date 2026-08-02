# Release checklist

Things that must be done outside this repository, or by hand, before a public
release. Recorded here because none of them can be satisfied by committing code.

## Before the first release

- [ ] **Make the repository public.** It is currently private, which means the
      HACS action's `archived`, `description`, and `topics` checks cannot be
      evaluated by anyone else, and the GitHub Actions API returns 404 to
      unauthenticated callers.
- [ ] **Set a repository description.** The HACS `description` check fails
      without one. Suggested: *Home Assistant integration for a self-hosted
      EinVault companion (pet) health and care tracker.*
- [ ] **Add repository topics.** The HACS `topics` check requires at least one.
      Suggested: `home-assistant`, `hacs`, `homeassistant-integration`,
      `einvault`, `pets`.
- [ ] **Confirm issues are enabled** — the HACS `issues` check requires it.
- [ ] **Submit the brand to `home-assistant/brands`.** This is a separate pull
      request adding `custom_integrations/einvault/` with `icon.png` (256x256)
      and `logo.png`. Until it is merged, the HACS `brands` check fails, which
      is why `.github/workflows/validate.yml` currently passes `ignore: brands`.
      **Remove that ignore once the brand is merged.**
- [ ] **Create the first tag** (`v0.1.0`). The release workflow rewrites
      `manifest.json`'s version from the tag and attaches `einvault.zip`.

## Rotate the development token

The API token used while building this integration was shared in a development
transcript. Rotate it in EinVault under *Settings → API tokens*; the
integration's reauth flow will prompt for the replacement.

The calendar feed token is separate and is regenerated from
*Settings → Calendar feed*.

## Known gaps, deliberately deferred

See `custom_components/einvault/quality_scale.yaml` for the full self
assessment. The items marked `todo` there are real and unhidden — notably
`parallel-updates`, `reconfiguration-flow`, `repair-issues`, and
`stale-devices`.

## Upstream

`docs/upstream-wishlist.md` holds fourteen issue-ready write-ups against
`davefatkin/EinVault`. W-13 (a quick log with no companion attached is silently
invisible to the API) and W-4 (the proxy rate-limit collapse) are the two most
worth filing first — both cost real debugging time during this build.
