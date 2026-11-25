"""Deck: the Ersa Compute Platform's management window (GTK 4 / libadwaita).

Pages: System (running version, boot health, slots), Updates, and Bundles (compat extensions and bundles). Deck holds no
policy and runs no commands: every action is a foundationd D-Bus call, authorized by polkit like the `foundation` CLI.
"""

__version__ = "0.1.0"
