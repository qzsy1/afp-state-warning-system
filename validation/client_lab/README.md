# AFP isolated client validation lab

This directory contains validation-only assets for exercising the public AFP workflow from a separate Windows user in Windows Sandbox. It does not modify product capture code, prediction code, or the five built-in PLC, ABB, thermocouple, pressure-film and UVC drivers.

## Host layout

`New-ClientLabStage.ps1` creates only `F:\AFP_Client_Validation_Lab`. The staged `input` folder is mapped read-only, while `results` is the only writable host mapping. The script pins the delivery helper and simulation fixture by SHA-256, copies only `bin`, `lib`, `share` and `LICENSE` from the portable MySQL tree, and refuses to overwrite an existing staged input or configuration.

A clean Sandbox may not contain the Microsoft Visual C++ runtime required by MySQL 8.4. The stage therefore requires the official x64 Redistributable to have a valid Microsoft Corporation Authenticode signature. It is installed only inside Sandbox. MySQL remains bound to `127.0.0.1`; an ephemeral Sandbox-only `init-file` creates `afp_app@127.0.0.1` because `root@127.0.0.1` cannot perform the first login while `skip-name-resolve=1` and only `root@localhost` exists. The bootstrap SQL is deleted after the application account passes its loopback check.

Windows Sandbox keeps its own system disk under Windows control. Closing the Sandbox, a Sandbox crash, or restarting Windows destroys files that have not been exported to the mapped results folder.

## Evidence boundary

The loopback TCP source and protocol helper generate synthetic validation data. Passing results prove the public browser/helper/MySQL transport path in an isolated Windows user; they do not prove any physical PLC, ABB controller, SMRF HID thermocouple, M3232 serial pressure film or UVC thermal camera.

Do not place browser passwords, pairing tokens, MySQL passwords, DPAPI ciphertext, API keys or raw private keys in source files or exported evidence. Physical-device and genuinely separate-computer acceptance remain `not_covered` until field evidence exists.
