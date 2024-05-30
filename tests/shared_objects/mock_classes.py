import threading

from vali_objects.utils.mdd_checker import MDDChecker
from vali_objects.utils.plagiarism_detector import PlagiarismDetector
from vali_objects.utils.challengeperiod_manager import ChallengePeriodManager
from vali_objects.utils.position_manager import PositionManager
from vali_objects.vali_dataclasses.perf_ledger import PerfLedgerManager
from shared_objects.cache_controller import CacheController

class MockMDDChecker(MDDChecker):
    def __init__(self, metagraph, position_manager, live_price_fetcher):
        lock = threading.Lock()
        super().__init__(None, metagraph, position_manager, lock, running_unit_tests=True,
                         live_price_fetcher=live_price_fetcher)

    # Lets us bypass the wait period in MDDChecker
    def get_last_update_time_ms(self):
        return 0

class MockPlagiarismDetector(PlagiarismDetector):
    def __init__(self, metagraph):
        super().__init__(None, metagraph, running_unit_tests=True)

    # Lets us bypass the wait period in PlagiarismDetector
    def get_last_update_time_ms(self):
        return 0
    
class MockChallengePeriodManager(ChallengePeriodManager):
    def __init__(self, metagraph):
        super().__init__(None, metagraph, running_unit_tests=True)

class MockPositionManager(PositionManager):
    def __init__(self, metagraph):
        super().__init__(None, metagraph, running_unit_tests=True)

class MockPerfLedgerManager(PerfLedgerManager):
    def __init__(self, metagraph):
        super().__init__(metagraph=metagraph, running_unit_tests=True)

class MockCacheController(CacheController):
    def __init__(self, metagraph):
        super().__init__(None, metagraph, running_unit_tests=True)

class MockMetagraph():
    def __init__(self, hotkeys):
        self.hotkeys = hotkeys
