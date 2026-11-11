import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main


class EntitySelectionTests(unittest.TestCase):
    @staticmethod
    def entity(entity_id, platform="portainer", disabled_by=None):
        return {
            "entity_id": entity_id,
            "platform": platform,
            "disabled_by": disabled_by,
        }

    @staticmethod
    def entity_ids(entities):
        return [entity["entity_id"] for entity in entities]

    def test_default_prefix_limits_targets_to_portainer_ix_entities(self):
        entities = [
            self.entity("sensor.ix_app_temperature"),
            self.entity("sensor.other_temperature"),
            self.entity("sensor.ix_external_temperature", platform="mqtt"),
        ]

        portainer, targeted, targets, kept_disabled, kept = (
            main.select_portainer_entities(entities, "ix_", []))

        self.assertEqual(self.entity_ids(portainer), [
            "sensor.ix_app_temperature", "sensor.other_temperature",
        ])
        self.assertEqual(self.entity_ids(targeted), ["sensor.ix_app_temperature"])
        self.assertEqual(self.entity_ids(targets), ["sensor.ix_app_temperature"])
        self.assertEqual(kept_disabled, [])
        self.assertEqual(kept, [])

    def test_custom_prefix_and_empty_prefix(self):
        entities = [
            self.entity("sensor.ix_app_temperature"),
            self.entity("sensor.app_temperature"),
            self.entity("sensor.other_temperature", platform="mqtt"),
        ]

        _, custom_targets, _, _, _ = main.select_portainer_entities(
            entities, "app_", [])
        _, all_portainer_targets, _, _, _ = main.select_portainer_entities(
            entities, "", [])

        self.assertEqual(self.entity_ids(custom_targets), ["sensor.app_temperature"])
        self.assertEqual(self.entity_ids(all_portainer_targets), [
            "sensor.ix_app_temperature", "sensor.app_temperature",
        ])

    def test_keep_matches_and_disabled_state_respect_enable_kept(self):
        entities = [
            self.entity("sensor.ix_app_health"),
            self.entity("sensor.ix_app_health_backup", disabled_by="user"),
            self.entity("sensor.ix_app_temperature"),
            self.entity("sensor.ix_app_old_temperature", disabled_by="user"),
        ]

        _, targeted, targets, kept_disabled, kept = (
            main.select_portainer_entities(entities, "ix_", ["health"]))
        _, _, _, not_enabled, _ = main.select_portainer_entities(
            entities, "ix_", ["health"])

        self.assertEqual(len(targeted), 4)
        self.assertEqual(self.entity_ids(targets), ["sensor.ix_app_temperature"])
        self.assertEqual(self.entity_ids(kept), [
            "sensor.ix_app_health", "sensor.ix_app_health_backup",
        ])
        self.assertEqual(self.entity_ids(kept_disabled), [
            "sensor.ix_app_health_backup",
        ])
        self.assertEqual(self.entity_ids(not_enabled), [
            "sensor.ix_app_health_backup",
        ])

    def test_parse_keep_normalizes_and_ignores_empty_items(self):
        self.assertEqual(main.parse_keep(" Health, oom, ,"), ["health", "oom"])


class GroupedOutputTests(unittest.TestCase):
    def test_shows_group_totals_and_clean_container_names(self):
        entities = [
            {"_cname": "ix_alpha", "_kind": "sensor.temperature"},
            {"_cname": "ix_alpha", "_kind": "sensor.temperature"},
            {"_cname": "ix_beta", "_kind": "sensor.temperature",
             "disabled_by": "user"},
        ]

        output = io.StringIO()
        with redirect_stdout(output):
            main.print_shape_grouped(entities, "keep")

        text = output.getvalue()
        self.assertIn("Group 1 of 1 | 2 container(s), 3 entities", text)
        self.assertIn("Kinds: sensor.temperature", text)
        self.assertIn("Containers: ix_alpha, ix_beta", text)
        self.assertNotIn("disabled_by", text)
        self.assertNotIn("entities)", text)

    def test_shows_disabled_source_only_when_not_user(self):
        entities = [
            {"_cname": "ix_alpha", "_kind": "sensor.temperature",
             "disabled_by": "user"},
            {"_cname": "ix_beta", "_kind": "sensor.temperature",
             "disabled_by": "integration"},
        ]

        output = io.StringIO()
        with redirect_stdout(output):
            main.print_shape_grouped(entities, "keep")

        text = output.getvalue()
        self.assertIn("Containers: ix_alpha, ix_beta [disabled_by: integration]", text)
        self.assertNotIn("ix_alpha [disabled_by: user]", text)

    def test_wraps_container_names_without_user_annotations(self):
        entities = [
            {"_cname": f"ix_managed_container_{index:02}",
             "_kind": "sensor.temperature", "disabled_by": "user"}
            for index in range(20)
        ]

        output = io.StringIO()
        with redirect_stdout(output):
            main.print_shape_grouped(entities, "disable")

        lines = output.getvalue().splitlines()
        self.assertTrue(any("20 container(s), 20 entities" in line for line in lines))
        self.assertTrue(all(len(line) <= 88 for line in lines if line.strip()))
        self.assertNotIn("disabled_by", output.getvalue())
        for index in range(20):
            self.assertIn(f"ix_managed_container_{index:02}", output.getvalue())

    def test_empty_group_says_there_are_no_containers(self):
        output = io.StringIO()
        with redirect_stdout(output):
            main.print_shape_grouped([], "enable")

        self.assertIn("No containers to enable.", output.getvalue())


class WouldEnablePreviewTests(unittest.TestCase):
    def test_flat_preview_shows_candidates_when_enable_flag_is_not_requested(self):
        entities = [{"entity_id": "sensor.ix_app_health", "disabled_by": "user"}]
        output = io.StringIO()

        with redirect_stdout(output):
            main.print_would_enable(entities, grouped=False, enable_requested=False)

        self.assertIn("COULD ENABLE (pass --enable-kept to enable)",
                      output.getvalue())
        self.assertIn("could enable: sensor.ix_app_health", output.getvalue())

    def test_grouped_preview_marks_enable_flag_as_requested(self):
        entities = [{"entity_id": "sensor.ix_app_health", "disabled_by": "user",
                     "_cname": "ix_app", "_kind": "sensor.health"}]
        output = io.StringIO()

        with redirect_stdout(output):
            main.print_would_enable(entities, grouped=True, enable_requested=True)

        self.assertIn("WOULD ENABLE (--enable-kept requested)", output.getvalue())
        self.assertIn("Group 1 of 1 | 1 container(s), 1 entities", output.getvalue())


class UndoSelectionTests(unittest.TestCase):
    def test_selects_only_existing_entities_in_expected_state(self):
        manifest_ops = [
            {"entity_id": "switch.kept", "was_disabled_by": "integration",
             "new_disabled_by": None},
            {"entity_id": "sensor.modified", "was_disabled_by": None,
             "new_disabled_by": "user"},
            {"entity_id": "sensor.gone", "was_disabled_by": None,
             "new_disabled_by": "user"},
        ]

        restore, missing, already_restored, changed = main.select_undo_ops(
            manifest_ops,
            {"switch.kept": None, "sensor.modified": "integration"},
        )

        self.assertEqual(restore, [
            {"entity_id": "switch.kept", "disabled_by": "integration"},
        ])
        self.assertEqual(missing, ["sensor.gone"])
        self.assertEqual(already_restored, [])
        self.assertEqual(changed, ["sensor.modified"])

    def test_selects_entities_already_at_their_original_state(self):
        manifest_ops = [{
            "entity_id": "sensor.restored",
            "was_disabled_by": None,
            "new_disabled_by": "user",
        }]

        restore, missing, already_restored, changed = main.select_undo_ops(
            manifest_ops, {"sensor.restored": None})

        self.assertEqual(restore, [])
        self.assertEqual(missing, [])
        self.assertEqual(already_restored, ["sensor.restored"])
        self.assertEqual(changed, [])


class UndoManifestTests(unittest.TestCase):
    def write_manifest(self, directory, filename, when, **extra):
        path = Path(directory) / filename
        manifest = {"when": when, "ops": [{"entity_id": "sensor.test"}]}
        manifest.update(extra)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_finds_newest_unfinished_manifest_by_run_time(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_manifest(directory, "undo-older.json", "2026-11-11T08:00:00")
            newest = self.write_manifest(
                directory, "undo-newer.json", "2026-11-11T08:10:00")
            self.write_manifest(
                directory, "undo-finished.json", "2026-11-11T08:20:00",
                undo_status="completed")
            self.write_manifest(
                directory, "undo-empty.json", "2026-11-11T08:30:00", ops=[])

            self.assertEqual(main.find_latest_undo_manifest(Path(directory)), newest)

    def test_returns_none_when_no_undoable_manifests_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_manifest(
                directory, "undo-finished.json", "2026-11-11T08:20:00",
                undo_status="completed")
            self.write_manifest(
                directory, "undo-empty.json", "2026-11-11T08:30:00", ops=[])

            self.assertIsNone(main.find_latest_undo_manifest(Path(directory)))

    def test_yes_keeps_manifest_unless_deletion_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "undo.json"
            manifest_path.write_text("{}", encoding="utf-8")

            deleted = main.maybe_delete_undo_manifest(
                manifest_path, assume_yes=True, delete_requested=False)

            self.assertFalse(deleted)
            self.assertTrue(manifest_path.exists())

    def test_interactive_confirmation_can_delete_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "undo.json"
            manifest_path.write_text("{}", encoding="utf-8")

            with patch("builtins.input", return_value="Y"):
                deleted = main.maybe_delete_undo_manifest(
                    manifest_path, assume_yes=False, delete_requested=False)

            self.assertTrue(deleted)
            self.assertFalse(manifest_path.exists())

    def test_delete_flag_deletes_manifest_without_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "undo.json"
            manifest_path.write_text("{}", encoding="utf-8")

            with patch("builtins.input", side_effect=AssertionError("prompted")):
                deleted = main.maybe_delete_undo_manifest(
                    manifest_path, assume_yes=True, delete_requested=True)

            self.assertTrue(deleted)
            self.assertFalse(manifest_path.exists())


class MutationSafetyTests(unittest.TestCase):
    def test_confirmation_accepts_y_and_case_insensitive_yes(self):
        for answer in ("y", "Y", "Yes", "yes", "YES"):
            with self.subTest(answer=answer), patch(
                    "builtins.input", return_value=answer):
                main.confirm_mutations(False, "Apply updates?")

    def test_confirmation_rejects_other_answers(self):
        for answer in ("n", "no", "yeah", ""):
            with self.subTest(answer=answer), patch(
                    "builtins.input", return_value=answer):
                with self.assertRaises(SystemExit):
                    main.confirm_mutations(False, "Apply updates?")

    def test_yes_flag_skips_prompt(self):
        with patch("builtins.input", side_effect=AssertionError("prompted")):
            main.confirm_mutations(True, "Apply updates?")

    def test_write_manifest_replaces_file_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "undo.json"
            manifest = {"status": "in_progress", "ops": []}

            main.write_manifest(manifest_path, manifest)

            self.assertEqual(json.loads(manifest_path.read_text()), manifest)
            self.assertFalse(manifest_path.with_suffix(".json.tmp").exists())


class ResponseTimeoutTests(unittest.TestCase):
    def test_send_times_out_when_response_never_arrives(self):
        class HangingWebSocket:
            async def send(self, payload):
                return None

            async def recv(self):
                await asyncio.Future()

        with patch.object(main, "RESPONSE_TIMEOUT_S", 0.01):
            with self.assertRaisesRegex(TimeoutError, "No response"):
                asyncio.run(main.send(HangingWebSocket(), 7, {"type": "test"}))

    def test_authentication_times_out_waiting_for_challenge_or_acceptance(self):
        class FakeWebSocket:
            def __init__(self, responses):
                self.responses = iter(responses)

            async def recv(self):
                try:
                    return next(self.responses)
                except StopIteration:
                    await asyncio.Future()

            async def send(self, payload):
                return None

        challenge = json.dumps({"type": "auth_required"})
        for responses in ([], [challenge]):
            with self.subTest(response_count=len(responses)):
                with patch.object(main, "RESPONSE_TIMEOUT_S", 0.01):
                    with self.assertRaisesRegex(TimeoutError, "Timed out"):
                        asyncio.run(main.authenticate(FakeWebSocket(responses), "token"))

    def test_authentication_rejects_unexpected_challenge(self):
        class FakeWebSocket:
            async def recv(self):
                return json.dumps({"type": "unexpected"})

            async def send(self, payload):
                raise AssertionError("must not send credentials")

        with self.assertRaisesRegex(SystemExit, "Unexpected.*challenge"):
            asyncio.run(main.authenticate(FakeWebSocket(), "token"))

    def test_authentication_reports_rejection(self):
        class FakeWebSocket:
            def __init__(self):
                self.responses = iter((
                    json.dumps({"type": "auth_required"}),
                    json.dumps({"type": "auth_invalid", "message": "denied"}),
                ))

            async def recv(self):
                return next(self.responses)

            async def send(self, payload):
                return None

        with self.assertRaisesRegex(SystemExit, "Auth failed.*auth_invalid"):
            asyncio.run(main.authenticate(FakeWebSocket(), "token"))


class UpdateBatchTests(unittest.TestCase):
    def test_failed_update_does_not_stop_later_updates(self):
        operations = [
            {"entity_id": "sensor.missing", "disabled_by": "user"},
            {"entity_id": "sensor.present", "disabled_by": "user"},
        ]

        async def run():
            with patch.object(main, "send", new_callable=AsyncMock) as send:
                send.side_effect = [
                    {"success": False, "error": {"message": "not found"}},
                    {"success": True},
                ]
                with redirect_stdout(io.StringIO()):
                    result = await main.apply_updates(None, 10, operations)
            return result, send

        (succeeded, failed), send = asyncio.run(run())
        self.assertEqual(succeeded, ["sensor.present"])
        self.assertEqual(failed, ["sensor.missing"])
        self.assertEqual(send.await_count, 2)


class JournalTests(unittest.TestCase):
    def test_connection_drop_marks_recovery_manifest_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "undo.json"
            operations = [
                {"entity_id": "sensor.first", "disabled_by": "user"},
                {"entity_id": "sensor.second", "disabled_by": "user"},
            ]
            manifest = {
                "status": "in_progress",
                "ops": [
                    {"entity_id": operation["entity_id"],
                     "was_disabled_by": None,
                     "new_disabled_by": operation["disabled_by"]}
                    for operation in operations
                ],
            }

            async def run():
                with patch.object(main, "send", new_callable=AsyncMock) as send:
                    send.side_effect = [
                        {"success": True},
                        ConnectionError("connection dropped"),
                    ]
                    with self.assertRaisesRegex(SystemExit, "Recovery manifest"):
                        await main.apply_journaled_updates(
                            None, 10, operations, manifest, manifest_path)
                return send

            send = asyncio.run(run())

            saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_manifest["status"], "interrupted")
            self.assertIn("connection dropped", saved_manifest["error"])
            self.assertEqual(send.await_count, 2)

    def test_failed_entity_updates_are_recorded_as_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "undo.json"
            manifest = {"status": "in_progress", "ops": []}

            async def run():
                with patch.object(
                        main, "apply_updates", new_callable=AsyncMock,
                        return_value=([], ["sensor.missing"])):
                    return await main.apply_journaled_updates(
                        None, 10, [], manifest, manifest_path)

            self.assertEqual(asyncio.run(run()), ([], ["sensor.missing"]))
            saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_manifest["status"], "completed")
            self.assertEqual(saved_manifest["failed"], ["sensor.missing"])


class MainSafetyTests(unittest.TestCase):
    class FakeWebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

    def run_main(self, args, manifest_dir, answer="no", entity=None,
                 entities=None):
        if entities is None:
            entities = [entity or {
            "entity_id": "sensor.ix_demo_temperature",
            "platform": "portainer",
            "disabled_by": None,
            }]

        async def fake_send(_ws, _msg_id, payload):
            if payload["type"] == "config/entity_registry/list":
                return {"success": True, "result": entities}
            if payload["type"] == "config/device_registry/list":
                return {"success": True, "result": []}
            if payload["type"] == "config/entity_registry/update":
                return {"success": True}
            raise AssertionError(f"Unexpected request: {payload}")

        send_mock = AsyncMock(side_effect=fake_send)
        output = io.StringIO()

        async def run():
            with (
                patch.object(main, "HA_TOKEN", "test-token"),
                patch.object(main, "MANIFESTS_DIR", manifest_dir),
                patch.object(main, "connect_and_auth",
                             new=AsyncMock(return_value=self.FakeWebSocket())),
                patch.object(main, "send", new=send_mock),
                patch("sys.argv", ["main.py", *args]),
                patch("builtins.input", return_value=answer),
                redirect_stdout(output),
            ):
                await main.main()

        asyncio.run(run())
        return send_mock, output.getvalue()

    def test_bare_undo_walks_newest_unfinished_manifest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_dir = Path(directory)
            older = manifest_dir / "undo-older.json"
            newer = manifest_dir / "undo-newer.json"
            older.write_text(json.dumps({
                "when": "2026-11-11T08:00:00",
                "ops": [{
                    "entity_id": "sensor.ix_demo_temperature",
                    "was_disabled_by": "integration",
                    "new_disabled_by": "user",
                }],
            }), encoding="utf-8")
            newer.write_text(json.dumps({
                "when": "2026-11-11T08:10:00",
                "ops": [{
                    "entity_id": "sensor.ix_demo_temperature",
                    "was_disabled_by": None,
                    "new_disabled_by": "user",
                }],
            }), encoding="utf-8")

            self.run_main(["-u"], manifest_dir, answer="no")
            first_undo = json.loads(newer.read_text(encoding="utf-8"))
            self.assertEqual(first_undo["undo_status"], "completed")
            self.assertNotIn("undo_status", json.loads(older.read_text()))

            self.run_main(["-u"], manifest_dir, answer="no")
            second_undo = json.loads(older.read_text(encoding="utf-8"))
            self.assertEqual(second_undo["undo_status"], "partial")

    def test_successful_undo_offers_and_deletes_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_dir = Path(directory)
            manifest_path = manifest_dir / "undo-latest.json"
            manifest_path.write_text(json.dumps({
                "when": "2026-11-11T08:10:00",
                "ops": [{
                    "entity_id": "sensor.ix_demo_temperature",
                    "was_disabled_by": "integration",
                    "new_disabled_by": None,
                }],
            }), encoding="utf-8")

            send_mock, _ = self.run_main(
                ["--undo"], manifest_dir, answer="yes")

            self.assertFalse(manifest_path.exists())
            self.assertIn("config/entity_registry/update", [
                call.args[2]["type"] for call in send_mock.await_args_list
            ])

    def test_dry_run_never_updates_or_writes_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_dir = Path(directory) / "manifests"

            send_mock, _ = self.run_main(["--dry-run"], manifest_dir)

            sent_types = [call.args[2]["type"] for call in send_mock.await_args_list]
            self.assertNotIn("config/entity_registry/update", sent_types)
            self.assertFalse(manifest_dir.exists())

    def test_renamed_grouping_and_inspection_flags_are_accepted(self):
        for args in (["--dry-run", "--by-kind"], ["--kinds"], ["--platforms"]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                send_mock, _ = self.run_main(args, Path(directory))
                self.assertTrue(send_mock.await_count)

    def test_short_prefix_keep_dry_run_and_group_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            send_mock, output = self.run_main(
                ["-n", "-g", "-p", "ix_", "-k", "temperature"],
                Path(directory))

        self.assertIn("Group 1 of 1 | 1 container(s), 1 entities", output)
        self.assertNotIn("config/entity_registry/update", [
            call.args[2]["type"] for call in send_mock.await_args_list
        ])

    def test_kept_sample_reports_how_many_entities_are_omitted(self):
        entities = [
            {
                "entity_id": f"sensor.ix_demo_health_{index:02}",
                "platform": "portainer",
                "disabled_by": None,
            }
            for index in range(12)
        ]
        with tempfile.TemporaryDirectory() as directory:
            _, output = self.run_main(
                ["--dry-run", "--keep", "health"], Path(directory),
                entities=entities)

        self.assertIn("Kept (sample):", output)
        self.assertIn("... and 2 more", output)

    def test_enable_candidates_are_only_updated_with_enable_kept_flag(self):
        entity = {
            "entity_id": "sensor.ix_demo_health",
            "platform": "portainer",
            "disabled_by": "user",
        }
        update_requests = []

        for args in (["--yes"], ["--enable-kept", "--yes"], ["-e", "-y"]):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                manifest_dir = Path(directory) / "manifests"
                with patch.dict("os.environ", {"HA_KEEP": "health"}):
                    send_mock, _ = self.run_main(
                        args, manifest_dir, entity=entity)
                update_requests.append([
                    call.args[2] for call in send_mock.await_args_list
                    if call.args[2]["type"] == "config/entity_registry/update"
                ])

        self.assertEqual(update_requests[0], [])
        for requests in update_requests[1:]:
            self.assertEqual(len(requests), 1)
            self.assertIsNone(requests[0]["disabled_by"])

    def test_declined_confirmation_never_updates_or_writes_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_dir = Path(directory) / "manifests"

            with self.assertRaisesRegex(SystemExit, "Cancelled"):
                self.run_main([], manifest_dir, answer="no")

            self.assertEqual(list(manifest_dir.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()