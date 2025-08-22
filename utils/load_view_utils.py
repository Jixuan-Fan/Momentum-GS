import threading
import queue
import random
from concurrent.futures import ThreadPoolExecutor
from utils.camera_utils import loadCam

class DataLoader:
    def __init__(self, args, cam_info_list, resolution_scale, queue_maxsize=20, num_workers=1, shuffle=False):
        self.args = args
        self.cam_info_list = cam_info_list
        self.resolution_scale = resolution_scale
        self.queue = queue.Queue(maxsize=queue_maxsize)
        self.num_workers = num_workers
        self.shuffle = shuffle
        self.total = len(cam_info_list)

        if self.total > 0:
            self.indexes = list(range(self.total))
            if self.shuffle:
                random.shuffle(self.indexes)
            self.idx = 0

            self.lock = threading.Lock()
            self.stop_event = threading.Event()
            self.executor = ThreadPoolExecutor(max_workers=num_workers)
            self.futures = []

            self._start_workers()

    def _worker(self):
        while not self.stop_event.is_set():
            with self.lock:
                if self.idx >= self.total:
                    self.indexes = list(range(self.total))
                    if self.shuffle:
                        random.shuffle(self.indexes)
                    self.idx = 0
                current_index = self.indexes[self.idx]
                self.idx += 1
            cam_info = self.cam_info_list[current_index]
            try:
                view = loadCam(self.args, current_index, cam_info, self.resolution_scale)
            except Exception as e:
                continue
            while not self.stop_event.is_set():
                try:
                    self.queue.put(view, timeout=1)
                    break
                except queue.Full:
                    continue

    def _start_workers(self):
        for _ in range(self.num_workers):
            future = self.executor.submit(self._worker)
            self.futures.append(future)

    def get_view(self, timeout=1):
        return self.queue.get(timeout=timeout)

    def shutdown(self):
        if self.total == 0:
            return
        self.stop_event.set()
        self.executor.shutdown(wait=True)

    def __iter__(self):
        return self

    def __next__(self):
        return self.get_view(timeout=1)
    
    def __len__(self):
        return self.total

    def __getitem__(self, index):
        if self.total == 0:
            raise IndexError("DataLoader is empty")

        if not isinstance(index, int):
            raise TypeError(f'Indices must be int or slice, not {type(index).__name__}')

        index %= self.total
        cam_info = self.cam_info_list[index]

        return loadCam(self.args, index, cam_info, self.resolution_scale)

    def __del__(self):
        try:
            self.shutdown()
        except Exception:
            pass

