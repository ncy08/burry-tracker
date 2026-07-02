# launchd templates

The extraction schedule itself is installed programmatically: `python -m
substack_trader install-service` generates and loads a plist with fifteen
`StartCalendarInterval` entries (8am, 1pm, and 6pm ET, Monday through Friday).
No template is needed for it.

`com.burry-tracker.redeploy.plist` is the optional daily Vercel redeploy job.
To install it, replace `__REPO__` with your clone's absolute path and
`__HOME__` with your home directory, copy it to `~/Library/LaunchAgents/`, and
run:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.burry-tracker.redeploy.plist
```
