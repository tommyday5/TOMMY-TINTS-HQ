import os
import tempfile
import unittest
from unittest.mock import patch
from assistant_memory import history, record_chat, clear_history, audit, audit_events, connect, MAX_CHAT_MESSAGES

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
    def test_chat_retention_keeps_latest_turns(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{"HQ_DATA_DIR":d}):
            for i in range(MAX_CHAT_MESSAGES // 2 + 3):
                record_chat("question "+str(i),"answer "+str(i))
            with connect() as db:
                rows=db.execute("SELECT role,content FROM chat_messages ORDER BY id").fetchall()
            self.assertEqual(len(rows),MAX_CHAT_MESSAGES)
            self.assertEqual(rows[0]["content"],"question 3")
            self.assertEqual(rows[-1]["content"],"answer "+str(MAX_CHAT_MESSAGES // 2 + 2))
            self.assertEqual(len(history()),12)
            self.assertEqual(len(audit_events()),50)

    def test_history_and_audit_limits_reject_unbounded_queries(self):
        for limit in (-1, 0, 201, True, "12", None):
            with self.subTest(history_limit=limit), self.assertRaises(ValueError):
                history(limit)
        for limit in (-1, 0, 101, False, "50", None):
            with self.subTest(audit_limit=limit), self.assertRaises(ValueError):
                audit_events(limit)

    def test_invalid_chat(self):
        for user_message, assistant_reply in (("", "answer"), ("   ", "answer"), ("question", "\\n  "), ("question", "")):
            with self.subTest(user_message=user_message, assistant_reply=assistant_reply):
                with self.assertRaises(ValueError):
                    record_chat(user_message, assistant_reply)
if __name__=="__main__":unittest.main()
