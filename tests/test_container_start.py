import json
import unittest
from unittest.mock import AsyncMock, patch

from tools import container_start as container_start_module


class ContainerStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_serial_number_payload(self):
        request_mock = AsyncMock(return_value="ok")

        with patch.object(
            container_start_module, "request_shopfloor", request_mock
        ):
            result = await container_start_module.container_start(
                mfg_order_name="MO-100076435",
                product_name="WM-900A",
                product_revision="A",
                qty=1,
                level_name="Unit",
                container_name="SN-0001",
                uom_name="EA",
            )

        self.assertEqual(result, "ok")
        self.assertEqual(request_mock.await_args.args, ("POST", "/api/Start"))
        self.assertEqual(
            request_mock.await_args.kwargs["body"],
            {
                "details": {
                    "mfgOrder": {"name": "MO-100076435"},
                    "product": {"name": "WM-900A", "revision": "A"},
                    "qty": 1,
                    "level": {"name": "Unit"},
                    "containerName": "SN-0001",
                    "autoNumber": False,
                    "uom": {"name": "EA"},
                }
            },
        )

    async def test_auto_number_payload(self):
        request_mock = AsyncMock(return_value="ok")

        with patch.object(
            container_start_module, "request_shopfloor", request_mock
        ):
            await container_start_module.container_start(
                mfg_order_name="MO-100076435",
                product_name="WM-900A",
                product_revision="A",
                qty=1,
                level_name="Unit",
                auto_number_rule_name="UNIT-SN",
            )

        details = request_mock.await_args.kwargs["body"]["details"]
        self.assertTrue(details["autoNumber"])
        self.assertEqual(
            details["autoNumberRule"], {"name": "UNIT-SN"}
        )
        self.assertNotIn("containerName", details)

    async def test_workflow_uses_current_status_details(self):
        request_mock = AsyncMock(return_value="ok")

        with patch.object(
            container_start_module, "request_shopfloor", request_mock
        ):
            await container_start_module.container_start(
                mfg_order_name="MO-100076435",
                product_name="WM-900A",
                product_revision="A",
                qty=1,
                level_name="Unit",
                container_name="SN-0003",
                workflow_name="WF-WM-900A",
                workflow_revision="A",
            )

        payload = request_mock.await_args.kwargs["body"]
        self.assertNotIn("workflow", payload["details"])
        self.assertEqual(
            payload["currentStatusDetails"]["workflow"],
            {"name": "WF-WM-900A", "revision": "A"},
        )

    async def test_numbering_mode_is_exclusive(self):
        request_mock = AsyncMock(return_value="ok")

        with patch.object(
            container_start_module, "request_shopfloor", request_mock
        ):
            missing = await container_start_module.container_start(
                "MO-1", "P-1", "A", 1, "Unit"
            )
            duplicate = await container_start_module.container_start(
                "MO-1",
                "P-1",
                "A",
                1,
                "Unit",
                container_name="SN-1",
                auto_number_rule_name="RULE-1",
            )

        self.assertIn("exactly one", missing)
        self.assertIn("exactly one", duplicate)
        request_mock.assert_not_awaited()

    async def test_body_json_deep_merges_case_insensitively(self):
        request_mock = AsyncMock(return_value="ok")
        body_json = json.dumps(
            {
                "Details": {
                    "Qty": 2,
                    "Product": {
                        "Name": "WM-900A",
                        "Revision": "B",
                    },
                },
                "Comments": "test",
            }
        )

        with patch.object(
            container_start_module, "request_shopfloor", request_mock
        ):
            await container_start_module.container_start(
                mfg_order_name="MO-100076435",
                product_name="WM-900A",
                product_revision="A",
                qty=1,
                level_name="Unit",
                container_name="SN-0002",
                body_json=body_json,
            )

        payload = request_mock.await_args.kwargs["body"]
        self.assertNotIn("Details", payload)
        self.assertNotIn("Qty", payload["details"])
        self.assertNotIn("Product", payload["details"])
        self.assertEqual(payload["details"]["qty"], 2)
        self.assertEqual(payload["details"]["product"]["revision"], "B")
        self.assertEqual(payload["comments"], "test")


if __name__ == "__main__":
    unittest.main()
