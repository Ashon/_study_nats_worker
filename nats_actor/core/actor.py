import asyncio
import logging
import signal
import time
from contextlib import asynccontextmanager
from contextlib import suppress

import uvloop

from nats_actor.core.utils import get_module
from nats_actor.core.utils import simple_eventloop
from nats_actor.core.driver import NatsDriver


WORKER_CONTROL_SIGNAL_START = 'START'
WORKER_CONTROL_SIGNAL_STOP = 'STOP'


def set_logger(conf):
    log_level = getattr(logging, conf.LOG_LEVEL, 'WARNING')
    logging.basicConfig(format=conf.LOG_FORMAT, level=log_level)


class Actor(object):
    conf = None

    # nats driver
    _driver = None

    # actor object's eventloop
    _loop = None

    # aio queue for actor lifecycle control
    _queue = None

    def __init__(self, conf):
        self.conf = conf

        set_logger(self.conf)
        logging.info('Initialize application')

        logging.debug('Setup driver')
        self._driver = NatsDriver([self.conf.NATS_URL])

        logging.debug('Setup uvloop')
        if self.conf.UVLOOP_ENABLED:
            uvloop.install()

        logging.debug('Prepare eventloop')
        self._loop = asyncio.get_event_loop()

        logging.debug('Generate worker')
        self._queue = asyncio.Queue()

        logging.debug('Set signal handler')
        self._handle_signal = self.create_signal_handler()
        signal.signal(signal.SIGTERM, self.stop)

    def stop(self):
        self._queue.put_nowait(WORKER_CONTROL_SIGNAL_STOP)

    def create_signal_handler(self):
        async def _handle_signal(msg):
            worker_message = msg.data.decode()
            logging.debug(f'Got worker signal [signal={worker_message}]')

            await self._queue.put_nowait(worker_message)
        return _handle_signal

    @asynccontextmanager
    async def nats_driver(self, loop=None):
        # remove condition for performance
        if not loop:
            loop = self._loop

        nats = await self._driver.get_connection(loop)

        yield nats

        # Gracefully unsubscribe the subscription
        await self._driver.close()

    async def _run_in_loop(self):
        async with self.nats_driver() as nats:

            # Setup worker lifecycle handler
            await nats.subscribe(self.conf.WORKER_NAME, cb=self._handle_signal)

            # Register tasks
            for task_spec in self.conf.TASKS:
                _, task_fn = get_module(task_spec['task'])
                callback = self._driver.create_task(task_fn)

                subscription_id = await nats.subscribe(
                    task_spec['subject'], queue=task_spec['queue'], cb=callback)

                logging.debug((
                    'Task is registered '
                    f'[subscription_id={subscription_id}]'
                    f'[subject={task_spec["subject"]}]'
                    f'[queue={task_spec["queue"]}]'
                    f'[task={task_fn.__name__}]'
                ))

            # wait for stop signal
            signal = WORKER_CONTROL_SIGNAL_START
            while signal != WORKER_CONTROL_SIGNAL_STOP:
                signal = await self._queue.get()

    def run(self):
        try:
            logging.info('Start - run worker')
            self._loop.run_until_complete(self._run_in_loop())

        except KeyboardInterrupt:
            logging.debug(f'Stop - send stop message to worker')
            self.stop()

        finally:
            logging.info('Stop - cancel pending eventloop tasks')
            pending_tasks = asyncio.Task.all_tasks()
            for task in pending_tasks:
                logging.debug((
                    f'[task={task.__class__.__name__}:{task.__hash__()}]'
                ))
                with suppress(asyncio.CancelledError):
                    self._loop.run_until_complete(task)
                    logging.debug(f'{task.__hash__()} [done={task.done()}]')

            logging.info('Stop - close eventloop')
            self._loop.close()

        logging.info('Bye')

    async def request(self, name, payload, loop=None):
        async with self.nats_driver(loop) as nats:
            res = await nats.request(name, payload)
            return res

    def send_task(self, name, payload):
        with simple_eventloop() as loop:
            now = time.perf_counter()

            response = loop.run_until_complete(
                self.request(name, payload, loop))

            elapsed = (time.perf_counter() - now) * 1000
            logging.debug(f'Request Finished. [elapsed={elapsed:.3f}]')

            return response
