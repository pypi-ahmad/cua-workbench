"""Make the ``docker/`` directory importable as a package.

The tests under ``tests/test_browser_bootstrap.py`` import
``docker.agent_service`` to exercise its helpers in-process (mocked
``shutil.which`` / ``subprocess.run``).  Without this file Python cannot
resolve the import and every test in that module fails with
``ModuleNotFoundError``.

The directory is still intended primarily as a container build context;
having ``__init__.py`` here has no effect on the runtime Docker image
(``entrypoint.sh`` invokes ``agent_service.py`` directly).
"""
