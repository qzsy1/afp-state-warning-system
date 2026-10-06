AFP Integrated System Modular v2.0.4
====================================
Start: AFP_Integrated_System_Modular.exe
Application logic is stored in app/modules and app/legacy and can be updated without rebuilding the EXE.
UI files are in app/ui; configuration is in config/runtime.json; prediction weights are in models.
Keep the complete directory together.  Another PC does not need Python, PyTorch or Git.
Use --module-status or --self-test for diagnostics; use --verify-files for SHA-256 integrity verification.
Use --mysql-smoke and --spreadsheet-smoke to verify database and Excel runtime dependencies.
Use verified patch ZIP files for module updates. Mutable logs, runtime data, update packages, verification outputs and Python caches are not in SHA256SUMS.txt.
