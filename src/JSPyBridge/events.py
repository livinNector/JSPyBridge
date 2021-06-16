import time, threading, json, sys
from . import connection, config
from queue import Queue


class TaskState:
    def __init__(self):
        self.stopping = False
        self.sleep = self.wait

    def wait(self, sec):
        stopTime = time.time() + sec
        while time.time() < stopTime and not self.stopping:
            time.sleep(0.2)
        if self.stopping:
            sys.exit(1)


class EventExecutorThread(threading.Thread):
    running = True
    jobs = Queue()
    completed = []
    doing = []

    def __init__(self):
        super().__init__()
        self.setDaemon(True)

    def add_job(self, request_id, cb_id, job, args):
        if request_id in self.doing:
            return  # We already are doing this
        self.doing.append(request_id)
        self.jobs.put([request_id, cb_id, job, args])

    def run(self):
        while self.running:
            request_id, cb_id, job, args = self.jobs.get()
            ok = job(args)
            if ok:
                self.completed.append(cb_id)
            if self.jobs.empty():
                self.doing = []


# The event loop here is shared across all threads. All of the IO between the
# JS and Python happens through this event loop. Because of Python's "Global Interperter Lock"
# only one thread can run Python at a time, so no race conditions to worry about.
class EventLoop:
    active = True
    sleepSeconds = 0.01

    callbackExecutor = EventExecutorThread()

    # This contains a map of callback request IDs to an object that can execute the callback.
    callbacks = {}

    # The threads created managed by this event loop.
    threads = []

    outbound = []

    # After a socket request is made, it's ID is pushed to self.requests. Then, after a response
    # is recieved it's removed from requests and put into responses, where it should be deleted
    # by the consumer.
    requests = {}  # Map of requestID -> threading.Lock
    responses = {}  # Map of requestID -> response payload

    def __init__(self):
        self.callbackExecutor.start()

    # === THREADING ===
    def newTaskThread(self, handler, *args):
        state = TaskState()
        t = threading.Thread(target=handler, args=(state, *args), daemon=True)
        self.threads.append([state, handler, t])
        t.start()
        return t

    def startThread(self, method):
        h = hash(method)
        self.newTaskThread(method)

    # Signal to the thread that it should stop. No forcing.
    def stopThread(self, method):
        for state, handler, thread in self.threads:
            if method == handler:
                state.stopping = True

    # Force the thread to stop -- if it doesn't kill after a set amount of time.
    def abortThread(self, method, killAfter=0.5):
        for state, handler, thread in self.threads:
            if handler == method:
                state.stopping = True
                killTime = time.time() + killAfter
                while thread.is_alive():
                    time.sleep(0.2)
                    if time.time() < killTime:
                        thread.terminate()

        self.threads = [x for x in self.threads if x[1] != method]

    # Stop the thread immediately
    def terminateThread(self, method):
        for state, handler, thread in self.threads:
            if handler == method:
                thread.terminate()
        self.threads = [x for x in self.threads if x[1] != method]

    # == IO ==

    # `queue_request` pushes this event onto the Payload
    def queue_request(self, request_id, payload, timeout=None):
        self.outbound.append(payload)
        lock = threading.Event()
        self.requests[request_id] = [lock, timeout]
        return lock

    def on_exit(self):
        if len(self.callbacks):
            config.debug('cannot exit because active callback', self.callbacks)
        while len(self.callbacks):
            time.sleep(0.2)
        self.callbackExecutor.running = False

    # === LOOP ===
    def loop(self):
        while self.active:
            # Send the next outbound request batch
            connection.writeAll(self.outbound)
            self.outbound = []

            # Iterate over the open threads and check if any have been killed, if so
            # remove them from self.threads
            self.threads = [x for x in self.threads if x[2].is_alive()]

            for r in self.callbackExecutor.completed:
                del self.callbacks[r]
            self.callbackExecutor.completed = []

            inbounds = connection.readAll()
            for inbound in inbounds:
                r = inbound["r"]
                cbid = inbound["cb"] if "cb" in inbound else None
                if r in self.requests:
                    lock, timeout = self.requests[r]
                    self.responses[r] = inbound
                    del self.requests[r]
                    lock.set()  # release, allow calling thread to resume
                if cbid in self.callbacks:
                    self.callbackExecutor.add_job(
                        r, cbid, self.callbacks[cbid]["internal"], inbound
                    )

            time.sleep(self.sleepSeconds)
