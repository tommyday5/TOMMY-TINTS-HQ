import os
import tempfile
import unittest
from unittest.mock import patch
from assistant_task_store import create_task, list_tasks, review_task

class AssistantTaskStoreTests(unittest.TestCase):
    def test_task_lifecycle_no_execution(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HQ_DATA_DIR":directory}):
            task=create_task({"title":"Follow up invoice","details":"Customer owes payment","category":"payments"})
            self.assertEqual(task["status"],"draft")
            self.assertEqual(len(list_tasks()),1)
            reviewed=review_task(task["id"],"approved")
            self.assertEqual(reviewed["status"],"approved")
            with self.assertRaises(ValueError):review_task(task["id"],"rejected")
            self.assertEqual(list_tasks()[0]["status"],"approved")
    def test_invalid_task(self):
        with self.assertRaises(ValueError):create_task({"title":"x"})
        with self.assertRaises(ValueError):create_task({"title":"Valid title","category":"unknown"})
if __name__=="__main__":unittest.main()
