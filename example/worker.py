import os
import sys
sys.path.append(os.getcwd()) # noqa E402

from nats_worker.core.worker import Worker
import settings

worker = Worker(settings)
worker.run()