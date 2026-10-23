# Startup Applications

This is a modern GTK/libadwaita application that shows a list of applications and services that are started when the computer starts. It looks into the following locations:

- `~/.config/autostart/`
- `/etc/xdg/autostart/`
- `/usr/share/gnome/autostart/`

It also looks in systemd unit files (both system and user) for services that are started when the computer starts.

## Order

The order is important. The app should first show the apps that are found in `~/.config/autostart/`, then the apps that are found in `/etc/xdg/autostart/`, and finally the apps that are found in `/usr/share/gnome/autostart/` and then systemd services.

## UI

The UI needs to be like all modern GNOME/libadwaita applications. It should show icons for the apps, if it cannot get an icon, it should use a nice, good looking placeholder icon.

It should also show the location of the autostart place (directory or systemd unit file) so user can easily understand why this app autostarts. It should also show the location of the executable.

## Inspiration

This app was originally inspired by a simple shell script that I wrote to find autostart applications, you can check it out here in project root: [find-autostart.sh](find-autostart.sh)
