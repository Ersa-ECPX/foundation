"""The Ersa Compute Platform updater: a GTK 4 / libadwaita window for installing a system update.

It holds no policy. It reads the state from foundationd over D-Bus (Boot.GetBootState, Boot.GetSlots, Updates.Pending),
hands the update file to Updates.Install as a file descriptor, and follows the job. foundationd asks polkit for the
administrator's password; the call allows interactive authorization, so the desktop's authentication dialog appears.
"""

__version__ = "0.1.0"
