# TODO

## Start the player after sign-in when it is already enabled

If the player is switched on before anyone has signed in, `start-jellyfin`
exits without a credential and the unit stays inactive. A later sign-in
through `jellyfin-setup` writes `users.json` but only restarts the player if
it was running, so it stays off until the toggle is switched once more.

Missing: in `jellyfin-setup`, after writing the credential, start
`jellyfin.service` when `systemctl --user is-enabled jellyfin.service`
says enabled, not only when it was active. Add a test next to the existing
stop→write→start tests in `tests/test_jellyfin_setup.py`.

## Sign out without a credential may scrub a temporary file

`scrub_leftovers` on Sign out also removes `.tmp` files next to
`users.json`. With no credential stored the player is not running, so
nothing writes there; the effect is negligible. Decide whether to restrict
the scrub to `.bak` files.

## config-server needs a restart after the configurator upgrade

The `text` setting type (configurator 2.19.0) is only served once
config-server restarts; the configurator package has no postinst restart.
The reboot that follows the first install (GPU access for the render group)
usually covers it. Fix belongs in the configurator package: restart
config-server in its postinst, or have it reload player descriptors.
