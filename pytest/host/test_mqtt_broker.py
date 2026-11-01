"""Host tier for the FR-029 test broker's config. Pure logic only."""
import stat

import mqtt_controller as mq


def test_fr029_broker_writes_its_own_log_file():
    # The log is what GET /api/mqtt/log serves: the only place a DUT's
    # CONNECT parameters (client id, clean session, keepalive) are visible.
    lines = mq.broker_config(1883).splitlines()
    assert f"log_dest file {mq.MOSQUITTO_LOG}" in lines
    assert "log_type all" in lines


def test_fr029_work_dir_is_writable_by_the_broker_group():
    # mosquitto drops to user `mosquitto` before opening its log, so the
    # work dir must let that user's group create files in it.
    assert mq.WORK_DIR_MODE & stat.S_IWGRP
    assert mq.WORK_DIR_MODE & stat.S_IXGRP
    assert not mq.WORK_DIR_MODE & stat.S_IWOTH


def test_fr029_broker_listens_open_on_the_port():
    lines = mq.broker_config(1883).splitlines()
    assert "listener 1883" in lines
    assert "allow_anonymous true" in lines
