# developer: jbonilla
# Copyright © 2024 Taoshi Inc
from copy import deepcopy
import random
from tests.shared_objects.mock_classes import MockMetagraph
from tests.vali_tests.base_objects.test_base import TestBase
from vali_config import TradePair
from vali_objects.position import Position
from vali_objects.utils.position_manager import PositionManager


class TestPositionManager(TestBase):

    def setUp(self):
        super().setUp()
        self.DEFAULT_MINER_HOTKEY = "test_miner"
        self.DEFAULT_POSITION_UUID = "test_position"
        self.DEFAULT_OPEN_MS = 1000
        self.DEFAULT_TRADE_PAIR = TradePair.BTCUSD
        self.default_position = Position(
            miner_hotkey=self.DEFAULT_MINER_HOTKEY,
            position_uuid=self.DEFAULT_POSITION_UUID,
            open_ms=self.DEFAULT_OPEN_MS,
            trade_pair=self.DEFAULT_TRADE_PAIR,
        )
        self.mock_metagraph = MockMetagraph([self.DEFAULT_MINER_HOTKEY])
        self.position_manager = PositionManager(metagraph=self.mock_metagraph, running_unit_tests=True)
        self.position_manager.init_cache_files()
        self.position_manager.clear_all_miner_positions_from_disk()

    def add_order_to_position_and_save_to_disk(self, position, order):
        position.add_order(order)
        self.position_manager.save_miner_position_to_disk(position)

    def _find_disk_position_from_memory_position(self, position):
        for disk_position in self.position_manager.get_all_miner_positions(position.miner_hotkey):
            if disk_position.position_uuid == position.position_uuid:
                return disk_position
        raise ValueError(f"Could not find position {position.position_uuid} in disk")

    def validate_positions(self, in_memory_position, expected_state):
        disk_position = self._find_disk_position_from_memory_position(in_memory_position)
        success, reason = PositionManager.positions_are_the_same(in_memory_position, expected_state)
        self.assertTrue(success, "In memory position is not as expected. " + reason)
        success, reason = PositionManager.positions_are_the_same(disk_position, expected_state)
        self.assertTrue(success, "Disc position is not as expected. " + reason)

    def test_creating_closing_and_fetching_multiple_positions(self):
        n_trade_pairs = len(TradePair)
        idx_to_position = {}
        # Create 6 orders per trade pair
        for i in range(n_trade_pairs):
            trade_pair = list(TradePair)[i]
            # Create 5 closed positions
            for j in range(5):
                position = deepcopy(self.default_position)
                position.position_uuid = f"{self.DEFAULT_POSITION_UUID}_{i}_{j}"
                position.open_ms = self.DEFAULT_OPEN_MS + 100 * i + j
                position.trade_pair = trade_pair
                position.close_out_position(position.open_ms + 1)
                idx_to_position[(i, j)] = position
                self.position_manager.save_miner_position_to_disk(position)
            # Create 1 open position
            j = 5
            position = deepcopy(self.default_position)
            position.position_uuid = f"{self.DEFAULT_POSITION_UUID}_{i}_{j}"
            position.open_ms = self.DEFAULT_OPEN_MS + 100 * i + j
            position.trade_pair = trade_pair
            idx_to_position[(i, j)] = position
            self.position_manager.save_miner_position_to_disk(position)

        all_disk_positions = self.position_manager.get_all_miner_positions(self.DEFAULT_MINER_HOTKEY, sort_positions=True)
        self.assertEqual(len(all_disk_positions), n_trade_pairs * 6)
        # Ensure the positions in all_disk_positions are sorted by close_ms.
        t0 = all_disk_positions[0].close_ms
        for i in range(n_trade_pairs):
            for j in range(6):
                n = i * 6 + j
                t1 = all_disk_positions[n].close_ms or float('inf')
                # Ensure the timestamp is increasing.
                self.assertTrue(t0 <= t1, 'timestamps not increasing or a valid timestamp came after a None timestamp')
                t0 = t1


        # Fetch all positions and verify that they are the same as the ones we created
        for i in range(n_trade_pairs):
            for j in range(6):
                expected_position = idx_to_position[(i, j)]
                disk_position = self._find_disk_position_from_memory_position(expected_position)
                self.validate_positions(expected_position, disk_position)

    def test_sorting_and_fetching_positions_with_random_close_times(self):
        num_positions = 100
        open_time_start = 1000
        open_time_end = 2000
        positions = []

        # Generate and save positions
        for i in range(num_positions):
            open_ms = random.randint(open_time_start, open_time_end)
            close_ms = None if random.choice([True, False]) else open_ms + random.randint(1, 1000)
            position = deepcopy(self.default_position)
            position.position_uuid = f"{self.DEFAULT_POSITION_UUID}_{i}"
            position.open_ms = open_ms
            position.close_ms = close_ms
            self.position_manager.save_miner_position_to_disk(position)
            positions.append(position)

        # Fetch and sort positions from disk
        all_disk_positions = self.position_manager.get_all_miner_positions(self.DEFAULT_MINER_HOTKEY,
                                                                           sort_positions=True)

        # Verify the number of positions fetched matches expectations
        self.assertEqual(len(all_disk_positions), num_positions)

        # Verify that positions are sorted correctly by close_ms, treating None as infinity
        for i in range(1, num_positions):
            prev_close_ms = all_disk_positions[i - 1].close_ms or float('inf')
            curr_close_ms = all_disk_positions[i].close_ms or float('inf')
            self.assertTrue(prev_close_ms <= curr_close_ms, "Positions are not sorted correctly by close_ms")


if __name__ == '__main__':
    import unittest

    unittest.main()
