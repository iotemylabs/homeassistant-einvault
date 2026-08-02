# Release checklist

Things that must be done outside this repository, or by hand, before a public
release. Recorded here because none of them can be satisfied by committing code.

## Before the first release

- [ ] **Make the repository public.** This is a hard blocker for HACS, not a
      nicety. HACS fetches `hacs.json` and `manifest.json` over *unauthenticated*
      `raw.githubusercontent.com`:

      ```python
      result = await self.hacs.async_download_file(
          f"https://raw.githubusercontent.com/{self.data.full_name}/{version}/hacs.json",
          nolog=True, handle_rate_limit=True,
      )
      return json_loads(result) if result else None
      ```

      A private repo returns 404 there, so `result` is `None` and both the
      `hacsjson` and `integration_manifest` checks fail with *"expected a
      dictionary. Got None"* — regardless of what those files contain. Both
      files have been validated against HACS's own schemas and are correct.
      Making the repo public is the only fix.
- [ ] **Set a repository description.** The HACS `description` check fails
      without one. Suggested: *Home Assistant integration for a self-hosted
      EinVault companion (pet) health and care tracker.*
- [ ] **Add repository topics.** The HACS `topics` check requires at least one,
      and currently fails. Settings → General → Topics. Suggested:
      `home-assistant`, `hacs`, `homeassistant-integration`, `einvault`, `pets`.
- [ ] **Confirm issues are enabled** — the HACS `issues` check requires it.
- [ ] **Submit the brand to `home-assistant/brands`.** This is a separate pull
      request adding `custom_integrations/einvault/` with `icon.png` (256x256)
      and `logo.png`. Until it is merged, the HACS `brands` check fails, which
      is why `.github/workflows/validate.yml` currently passes `ignore: brands`.
      **Remove that ignore once the brand is merged.**
- [ ] **Create the first tag** (`v0.1.0`). The release workflow rewrites
      `manifest.json`'s version from the tag and attaches `einvault.zip`.
- [ ] **Optionally re-enable `zip_release`.** `hacs.json` deliberately omits it
      for now: `zip_release: true` makes HACS install from a release asset, and
      with no releases published that install fails. Once tagged releases exist,
      adding `"zip_release": true` and `"filename": "einvault.zip"` back makes
      installs smaller and faster.

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
