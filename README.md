# bms_restore_joystick_xmls
Python tool that restores Falcon BMS joystick bindings. It creates timestamped ZIP snapshots of the full BMS config folder and automatically restores control assignments when Windows changes device GUIDs by copying known-good backup content into newly created config files.
