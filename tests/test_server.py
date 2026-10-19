import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class LocalApiFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kol-town-test-")
        self.store = server.Store(Path(self.temp.name) / "test.db")
        self.httpd = server.KolTownServer(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, method="GET", body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if method != "GET":
            headers.update({
                "Content-Type": "application/json",
                "X-KOL-Town": "local-demo",
                "Origin": self.base,
            })
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with self.client.open(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_complete_local_account_and_game_services_flow(self):
        status, bootstrap = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertFalse(bootstrap["session"]["authenticated"])
        self.assertTrue(bootstrap["capabilities"]["server_progress"])
        self.assertTrue(bootstrap["capabilities"]["local_kol_chat"])
        self.assertFalse(bootstrap["capabilities"]["chat_raw_text_retained"])
        self.assertFalse(bootstrap["capabilities"]["real_reward"])

        demo_state = server.default_state()
        demo_state.update({
            "accessChosen": True,
            "onboardingComplete": True,
            "bricks": 135,
            "totalEarned": 135,
            "prosperity": 120,
            "totalProsperity": 120,
            "level": 2,
            "completedTasks": ["quiz", "live", "story"],
            "airdropTokens": 2,
            "airdropBrickCredits": 10,
            "airdropFlightsSeen": 1,
        })
        status, registered = self.request("/api/auth/register", "POST", {
            "alias": "LocalQA",
            "pin": "2468",
            "consent": True,
            "progress": demo_state,
        })
        self.assertEqual(status, 201)
        self.assertEqual(registered["confirmation_source"], "local_demo_server")
        self.assertEqual(registered["user"]["role"], "player")
        self.assertEqual(registered["progress"]["airdropTokens"], 2)
        self.assertEqual(registered["progress"]["airdropBrickCredits"], 10)

        expanded_look = dict(registered["progress"])
        expanded_look["avatar"] = {"hair": "cloud-pony", "outfit": "stage", "accessory": "crystal"}
        status, saved_look = self.request("/api/progress", "PUT", {"state": expanded_look})
        self.assertEqual(status, 200)
        self.assertEqual(saved_look["progress"]["avatar"], expanded_look["avatar"])

        business_state = dict(saved_look["progress"])
        business_state.update({
            "businessRevenue": 800,
            "brandReputation": 12,
            "brandOrders": {"nova-download": "settled", "nova-register": "accepted", "nova-deposit": "accepted"},
            "activeBrandOrder": "nova-register",
            "growthProofs": {"nova-download": True, "nova-register": True, "nova-deposit": True},
            "businessLedger": [{
                "id": "biz_test", "label": "Campaign 本地验收 · 拉新", "delta": 800,
                "note": "下载路径演练完成 · 外部 KPI 未确认", "at": server.utc_now(),
            }],
        })
        status, saved_business = self.request("/api/progress", "PUT", {"state": business_state})
        self.assertEqual(status, 200)
        self.assertEqual(saved_business["progress"]["businessRevenue"], 800)
        self.assertEqual(saved_business["progress"]["brandReputation"], 12)
        self.assertEqual(saved_business["progress"]["activeBrandOrder"], "nova-register")
        self.assertEqual(saved_business["progress"]["brandOrders"]["nova-download"], "settled")
        self.assertEqual(saved_business["progress"]["brandOrders"]["nova-deposit"], "available")
        self.assertTrue(saved_business["progress"]["growthProofs"]["nova-register"])
        self.assertEqual(saved_business["progress"]["businessLedger"][0]["delta"], 800)

        event = {
            "event_id": "evt_local_api_test",
            "event_name": "registration_complete",
            "event_time": server.utc_now(),
            "campaign_id": server.CAMPAIGN_ID,
            "contract_version": server.CONTRACT_VERSION,
            "playable_id": server.PLAYABLE_ID,
            "playable_config_version": server.PLAYABLE_CONFIG_VERSION,
            "build_id": server.BUILD_ID,
            "properties": {"confirmation_source": "local_demo_server"},
        }
        status, accepted = self.request("/api/events", "POST", {"event": event})
        self.assertEqual(status, 202)
        self.assertTrue(accepted["accepted"])
        status, duplicate = self.request("/api/events", "POST", {"event": event})
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])

        status, attribution = self.request("/api/attribution/click", "POST", {
            "campaign_id": server.CAMPAIGN_ID,
            "creative_id": "cr_test",
            "channel_id": "ch_test",
            "kol_id": "kol_demo_luna",
            "link_id": "link_test",
        })
        self.assertEqual(status, 201)
        self.assertTrue(attribution["click_id"].startswith("clk_"))

        status, invite = self.request("/api/invite/bind", "POST", {"code": "LUNA2026"})
        self.assertEqual(status, 200)
        self.assertTrue(invite["progress"]["inviteBound"])

        status, chat = self.request("/api/chat", "POST", {"message": "我今天做什么？"})
        self.assertEqual(status, 200)
        self.assertEqual(chat["persona"]["mode"], "local_rules")
        self.assertFalse(chat["persona"]["real_person_live"])
        self.assertEqual(chat["intent"], "task_recommendation")
        self.assertEqual(chat["action"]["type"], "open_task")
        self.assertFalse(chat["privacy"]["raw_text_retained"])
        self.assertFalse(chat["privacy"]["remote_model"])

        secret_prompt = "我的助记词不能被保存 qa-secret-phrase"
        status, blocked_chat = self.request("/api/chat", "POST", {"message": secret_prompt})
        self.assertEqual(status, 200)
        self.assertTrue(blocked_chat["blocked"])
        self.assertEqual(blocked_chat["intent"], "sensitive_data")
        with self.store.connect() as database:
            database_dump = "\n".join(database.iterdump())
        self.assertNotIn(secret_prompt, database_dump)

        status, reward = self.request("/api/rewards/redeem", "POST", {"reward_id": "local_demo_coffee"})
        self.assertEqual(status, 200)
        self.assertEqual(reward["claim_status"], "local_demo_card")
        self.assertEqual(reward["fulfillment"], "none")
        self.assertEqual(reward["progress"]["bricks"], 75)
        self.assertIn("coffee", reward["progress"]["benefits"])

        status, leaderboard = self.request("/api/leaderboard")
        self.assertEqual(status, 200)
        self.assertEqual(leaderboard["source"], "local_sqlite_server")
        self.assertEqual(leaderboard["total"][0]["name"], "LocalQA")
        self.assertTrue(leaderboard["total"][0]["is_me"])

        status, logged_out = self.request("/api/auth/logout", "POST", {})
        self.assertEqual(status, 200)
        self.assertIsNone(logged_out["user"])
        status, logged_in = self.request("/api/auth/login", "POST", {
            "alias": "LocalQA", "pin": "2468", "progress": server.default_state()
        })
        self.assertEqual(status, 200)
        self.assertEqual(logged_in["progress"]["bricks"], 75)

        status, deleted = self.request("/api/auth/account", "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(deleted["deleted"])
        self.assertFalse(deleted["progress"]["accessChosen"])
        status, final_bootstrap = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertFalse(final_bootstrap["session"]["authenticated"])
        self.assertFalse(final_bootstrap["progress"]["accessChosen"])

        self.store.set_config("kill_switch", "on")
        status, health = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["kill_switch"])


if __name__ == "__main__":
    unittest.main()
