import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import chineseinla_renovation as app


class RuntimeTests(unittest.TestCase):
    def test_exception_exits_without_retry(self):
        with patch.object(app, "STOP_EVENT", threading.Event()), patch.object(app, "main", side_effect=RuntimeError("test failure")) as run:
            self.assertEqual(app.run_once(), 1)
            run.assert_called_once()

    def test_once_exit_status(self):
        for success, status in ((True, 0), (False, 1)):
            with patch.object(app, "STOP_EVENT", threading.Event()), \
                    patch.object(app, "main", return_value=success):
                self.assertEqual(app.run_once(), status)

    def test_stop_finishes_round_without_another_wait(self):
        event = threading.Event()

        def run():
            app.request_stop(None, None)
            return True

        with patch.object(app, "STOP_EVENT", event), \
                patch.object(app, "main", side_effect=run), \
                patch.object(event, "wait") as wait:
            self.assertEqual(app.run_once(), 0)
            wait.assert_not_called()

    def test_log_redaction_and_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            app.configure_logging(log_dir=directory)
            try:
                handler = next(h for h in app.LOGGER.handlers if isinstance(h, app.RotatingFileHandler))
                handler.maxBytes = 180
                with patch.object(app, "NOTION_TOKEN", "private-test-token"):
                    for number in range(12):
                        app.LOGGER.warning("record %s private-test-token", number)
                files = list(Path(directory).glob("chineseinla.log*"))
                self.assertGreater(len(files), 1)
                self.assertLessEqual(len(files), 6)
                for path in files:
                    content = path.read_text(encoding="utf-8")
                    self.assertNotIn("private-test-token", content)
                    self.assertIn("[REDACTED]", content)
            finally:
                for handler in app.LOGGER.handlers[:]:
                    handler.close()
                    app.LOGGER.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
