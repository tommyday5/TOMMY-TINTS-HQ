import os
import tempfile
import unittest
from unittest.mock import patch
from assistant_memory import history, record_chat, clear_history, audit, audit_events

class AssistantMemoryTests(unittest.TestCase):
    def test_history_and_audit(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{"HQ_DATA_DIR":d}):
            record_chat("Hello","How can I help?")
            self.assertEqual(history(),[{"role":"user","content":"Hello"},{"role":"assistant","content":"How can I help?"}])
            audit("task_created","Draft created")
            self.assertEqual(audit_events()[0]["event"],"task_created")
            clear_history()
            self.assertEqual(history(),[])
            self.assertEqual(audit_events()[0]["event"],"chat_cleared")
    def test_invalid_chat(self):
        with self.assertRaises(ValueError):record_chat("","answer")
if __name__=="__main__":unittest.main()
