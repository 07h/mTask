# mTask.py

import asyncio
import inspect
import json
import logging
import uuid
from functools import wraps
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional, List

from croniter import croniter
import redis.asyncio as redis
from pydantic import BaseModel
import threading

# ============================
# Custom Exception Classes
# ============================


class mTaskError(Exception):
    """Base exception class for mTask."""

    pass


class RedisConnectionError(mTaskError):
    """Raised when there is a connection error with Redis."""

    pass


class TaskEnqueueError(mTaskError):
    """Raised when enqueueing a task fails."""

    pass


class TaskDequeueError(mTaskError):
    """Raised when dequeueing a task fails."""

    pass


class TaskRequeueError(mTaskError):
    """Raised when requeueing a task fails."""

    pass


class TaskProcessingError(mTaskError):
    """Raised when processing a task fails."""

    pass


class TaskFunctionNotFoundError(mTaskError):
    """Raised when the task function is not found in the registry."""

    pass


# ============================
# TaskQueue Class
# ============================


class TaskQueue:
    """
    Manages task queues using Redis with support for processing queues.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize the TaskQueue.

        Args:
            redis_url (str): Redis connection URL.
            logger (logging.Logger, optional): Logger instance for logging.
        """
        self.redis_url = redis_url
        self.redis: Optional[redis.Redis] = None
        self.logger = logger or logging.getLogger("TaskQueue")

    async def connect(self):
        """
        Establish a connection to Redis.

        Raises:
            RedisConnectionError: If unable to connect to Redis.
        """
        try:
            self.redis = redis.from_url(
                self.redis_url, encoding="utf-8", decode_responses=True
            )
            await self.redis.ping()
            self.logger.info(f"Connected to Redis at {self.redis_url}")
        except Exception as e:
            self.logger.exception(f"Failed to connect to Redis: {e}")
            raise RedisConnectionError(f"Failed to connect to Redis: {e}") from e

    async def disconnect(self):
        """
        Close the connection to Redis.
        """
        if self.redis:
            await self.redis.close()
            self.logger.info("Disconnected from Redis")

    async def enqueue(
        self,
        queue_name: str,
        kwargs: Dict[str, Any] = None,
    ) -> str:
        """
        Add a task to the specified Redis main queue.

        Args:
            queue_name (str): Name of the Redis queue.
            kwargs (Dict[str, Any], optional): Keyword arguments for the task.

        Returns:
            str: Unique identifier of the enqueued task.

        Raises:
            TaskEnqueueError: If enqueueing the task fails.
            RedisConnectionError: If Redis is not connected.
        """
        if not self.redis:
            self.logger.error("Redis is not connected.")
            raise RedisConnectionError("Redis is not connected.")

        task = {
            "id": str(uuid.uuid4()),
            "name": queue_name,  # 'name' corresponds to 'queue_name'
            "kwargs": kwargs or {},
            "status": "pending",
            "retry_count": 0,
        }

        try:
            await self.redis.rpush(queue_name, json.dumps(task))
            self.logger.debug(f"Enqueued task {task['id']} to queue '{queue_name}'")
            return task["id"]
        except Exception as e:
            self.logger.exception(f"Failed to enqueue task: {e}")
            raise TaskEnqueueError(f"Failed to enqueue task: {e}") from e

    async def dequeue(self, queue_name: str = "default") -> Optional[Dict[str, Any]]:
        """
        Atomically move a task from the main queue to the processing queue and retrieve it.

        Args:
            queue_name (str, optional): Name of the Redis queue.

        Returns:
            Optional[Dict[str, Any]]: The task data or None if the queue is empty.

        Raises:
            TaskDequeueError: If dequeueing the task fails.
            RedisConnectionError: If Redis is not connected.
        """
        if not self.redis:
            self.logger.error("Redis is not connected.")
            raise RedisConnectionError("Redis is not connected.")

        processing_queue = f"{queue_name}:processing"
        try:
            # BLPOP: Blocking pop from the left end (FIFO)
            task_tuple = await self.redis.blpop(queue_name, timeout=5)
            if task_tuple:
                _, task_json = task_tuple
                # Do not modify the task
                # Add task to processing queue
                await self.redis.rpush(processing_queue, task_json)
                task = json.loads(task_json)
                self.logger.debug(
                    f"Dequeued task {task['id']} with name '{task['name']}' from queue '{queue_name}' to processing queue '{processing_queue}'"
                )
                return task
            return None
        except Exception as e:
            self.logger.exception(
                f"Failed to dequeue task from queue '{queue_name}': {e}"
            )
            raise TaskDequeueError(
                f"Failed to dequeue task from queue '{queue_name}': {e}"
            ) from e

    async def _update_task_in_processing_queue(
        self, processing_queue: str, task: Dict[str, Any]
    ):
        """
        Update the task in the processing queue with new data.

        Args:
            processing_queue (str): Name of the processing queue.
            task (Dict[str, Any]): The task data to update.
        """
        try:
            # Retrieve all tasks in the processing queue
            tasks = await self.redis.lrange(processing_queue, 0, -1)
            for index, task_json in enumerate(tasks):
                existing_task = json.loads(task_json)
                if existing_task["id"] == task["id"]:
                    # Update the task at the specific index
                    await self.redis.lset(processing_queue, index, json.dumps(task))
                    break
        except Exception as e:
            self.logger.exception(f"Failed to update task in processing queue: {e}")

    async def requeue(self, task: Dict[str, Any], queue_name: str = "default") -> None:
        """
        Re-add a task to the specified Redis main queue for retrying.

        Args:
            task (Dict[str, Any]): The task data.
            queue_name (str, optional): Name of the Redis queue.

        Raises:
            TaskRequeueError: If requeueing the task fails.
            RedisConnectionError: If Redis is not connected.
        """
        if not self.redis:
            self.logger.error("Redis is not connected.")
            raise RedisConnectionError("Redis is not connected.")

        task["status"] = "pending"
        # Remove start_time when requeuing
        task.pop("start_time", None)
        try:
            await self.redis.rpush(queue_name, json.dumps(task))
            self.logger.debug(
                f"Requeued task {task['id']} to queue '{queue_name}' (retry {task['retry_count']})"
            )
        except Exception as e:
            self.logger.exception(f"Failed to requeue task '{task['id']}': {e}")
            raise TaskRequeueError(f"Failed to requeue task '{task['id']}': {e}") from e

    async def mark_completed(self, task_id: str, queue_name: str) -> None:
        """
        Remove a task from the processing queue after successful execution.

        Args:
            task_id (str): Unique identifier of the task.
            queue_name (str): Name of the Redis queue.

        Raises:
            TaskProcessingError: If marking the task as completed fails.
        """
        processing_queue = f"{queue_name}:processing"
        try:
            # Retrieve all tasks in the processing queue
            tasks = await self.redis.lrange(processing_queue, 0, -1)
            for task_json in tasks:
                task = json.loads(task_json)
                if task["id"] == task_id:
                    # Remove the task using the exact task_json
                    await self.redis.lrem(processing_queue, 0, task_json)
                    self.logger.info(
                        f"Task {task_id} marked as completed and removed from processing queue '{processing_queue}'."
                    )
                    break
        except Exception as e:
            self.logger.exception(f"Failed to mark task {task_id} as completed: {e}")
            raise TaskProcessingError(
                f"Failed to mark task {task_id} as completed: {e}"
            ) from e

    async def recover_processing_tasks(self, queue_name: str):
        """
        Recover tasks from the processing queue back to the main queue at startup.

        Args:
            queue_name (str): Name of the Redis queue.
        """
        processing_queue = f"{queue_name}:processing"
        main_queue = queue_name
        try:
            # Get all tasks from processing queue
            tasks = await self.redis.lrange(processing_queue, 0, -1)
            if tasks:
                # Move tasks back to main queue (to the front)
                await self.redis.lpush(main_queue, *tasks[::-1])
                await self.redis.delete(processing_queue)
                self.logger.info(
                    f"Recovered {len(tasks)} tasks from processing queue '{processing_queue}' back to main queue '{main_queue}'."
                )
        except Exception as e:
            self.logger.exception(
                f"Failed to recover tasks from processing queue '{processing_queue}': {e}"
            )

    async def get_task_count(self, queue_name: str) -> int:
        """
        Get the number of tasks in the main queue.

        Args:
            queue_name (str): Name of the Redis queue.

        Returns:
            int: Number of tasks in the queue.
        """
        try:
            count = await self.redis.llen(queue_name)
            return count
        except Exception as e:
            self.logger.error(f"Failed to get task count for '{queue_name}': {e}")
            return 0

    async def get_processing_task_count(self, queue_name: str) -> int:
        """
        Get the number of tasks in the processing queue.

        Args:
            queue_name (str): Name of the Redis queue.

        Returns:
            int: Number of tasks in the processing queue.
        """
        processing_queue = f"{queue_name}:processing"
        try:
            count = await self.redis.llen(processing_queue)
            return count
        except Exception as e:
            self.logger.error(
                f"Failed to get processing task count for '{queue_name}': {e}"
            )
            return 0


# ============================
# Worker Class
# ============================


class Worker:
    """
    Processes tasks from a specific Redis queue.
    """

    def __init__(
        self,
        task_queue: TaskQueue,
        task_registry: Dict[str, Dict[str, Any]],
        async_task_registry_lock: asyncio.Lock,
        retry_limit: int = 3,
        queue_name: str = "default",
        semaphore: Optional[asyncio.Semaphore] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize the Worker.

        Args:
            task_queue (TaskQueue): Instance of TaskQueue.
            task_registry (Dict[str, Dict[str, Any]]): Registry of task functions and their concurrency.
            async_task_registry_lock (asyncio.Lock): Async lock for accessing task_registry.
            retry_limit (int, optional): Max retry attempts for failed tasks.
            queue_name (str, optional): Name of the Redis queue to process.
            semaphore (asyncio.Semaphore, optional): Semaphore to limit concurrency.
            logger (logging.Logger, optional): Logger instance for logging.
        """
        self.task_queue = task_queue
        self.retry_limit = retry_limit
        self.queue_name = queue_name
        self.task_registry = task_registry
        self.async_task_registry_lock = async_task_registry_lock
        self.semaphore = semaphore if semaphore else asyncio.Semaphore(1)
        self._running = False
        self._workers: List[asyncio.Task] = []
        self.logger = logger or logging.getLogger(f"Worker-{queue_name}")

        self.logger.debug(
            f"Worker initialized with concurrency={self.semaphore._value} for queue '{queue_name}'"
        )

    async def start(self):
        """
        Start the worker coroutines based on concurrency.
        """
        if self._running:
            self.logger.warning("Worker is already running.")
            return

        self._running = True
        concurrency = self.semaphore._value
        self.logger.info(
            f"Starting {concurrency} worker(s) for queue '{self.queue_name}'"
        )

        for i in range(concurrency):
            monitor_task = asyncio.create_task(self._monitor_worker(i + 1))
            self._workers.append(monitor_task)

    async def _monitor_worker(self, worker_id: int):
        """
        Monitors a worker loop and restarts it if it ends unexpectedly.
        """
        while self._running:
            worker_task = asyncio.create_task(self._worker_loop(worker_id))
            try:
                await worker_task
            except asyncio.CancelledError:
                self.logger.info(f"Worker {worker_id}: Received shutdown signal.")
                break
            except Exception as e:
                self.logger.exception(f"Worker {worker_id}: Unexpected error: {e}")
                # Optionally, wait before restarting to prevent rapid restarts
                await asyncio.sleep(1)
                self.logger.info(
                    f"Worker {worker_id}: Restarting after unexpected termination."
                )
            finally:
                # Remove the completed task from the list
                if worker_task in self._workers:
                    self._workers.remove(worker_task)

    async def _worker_loop(self, worker_id: int):
        """
        Main loop for each worker coroutine.
        """
        while self._running:
            try:
                async with self.semaphore:
                    task = await self.task_queue.dequeue(queue_name=self.queue_name)
                    if task:
                        self.logger.info(
                            f"Worker {worker_id}: Dequeued task {task['id']} from queue '{self.queue_name}'"
                        )
                        await self.process_task(task, worker_id)
                    else:
                        await asyncio.sleep(1)  # Prevent busy waiting
            except asyncio.CancelledError:
                self.logger.info(f"Worker {worker_id}: Received shutdown signal.")
                break
            except Exception as e:
                self.logger.exception(f"Worker {worker_id}: Error in worker loop: {e}")
                # Optionally, handle specific exceptions or implement retry logic
                await asyncio.sleep(1)  # Wait before retrying

    async def stop(self):
        """
        Stop all worker coroutines.
        """
        if not self._running:
            self.logger.warning("Worker is not running.")
            return

        self._running = False
        self.logger.info("Stopping workers...")

        for worker_task in self._workers:
            worker_task.cancel()

        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self.logger.info("Workers stopped.")

    async def _worker_loop(self, worker_id: int):
        """
        Main loop for each worker coroutine.

        Args:
            worker_id (int): Identifier for the worker.
        """
        while self._running:
            try:
                async with self.semaphore:
                    task = await self.task_queue.dequeue(queue_name=self.queue_name)
                    if task:
                        self.logger.info(
                            f"Worker {worker_id}: Dequeued task {task['id']} from queue '{self.queue_name}'"
                        )
                        await self.process_task(task, worker_id)
                    else:
                        await asyncio.sleep(1)  # Prevent busy waiting
            except asyncio.CancelledError:
                self.logger.info(f"Worker {worker_id}: Received shutdown signal.")
                break
            except Exception as e:
                self.logger.exception(f"Worker {worker_id}: Unexpected error: {e}")
                # Depending on the error, you may choose to continue or stop

    async def process_task(self, task: Dict[str, Any], worker_id: int):
        queue_name = task["name"]  # 'name' соответствует 'queue_name'
        kwargs = task.get("kwargs", {})
        async with self.async_task_registry_lock:
            task_info = self.task_registry.get(queue_name, {})
            func = task_info.get("func")  # Получаем функцию
            timeout = task_info.get("timeout")  # Получаем тайм-аут для задачи
        if not func:
            self.logger.error(f"Task function for queue '{queue_name}' not found.")
            raise TaskFunctionNotFoundError(
                f"Task function for queue '{queue_name}' not found."
            )

        try:
            task["start_time"] = datetime.utcnow().timestamp()
            self.logger.info(
                f"Worker {worker_id}: Executing task {task['id']} from queue '{queue_name}'"
            )

            # Проверяем, ожидает ли функция Pydantic модель
            sig = inspect.signature(func)
            expects_model = False
            model_class: Optional[BaseModel] = None
            for name, param in sig.parameters.items():
                if (
                    param.annotation
                    and inspect.isclass(param.annotation)
                    and issubclass(param.annotation, BaseModel)
                ):
                    expects_model = True
                    model_class = param.annotation
                    break

            # Выполняем задачу с тайм-аутом
            if expects_model and model_class:
                # Десериализуем kwargs в Pydantic модель
                data_model = model_class(**kwargs)
                self.logger.info(
                    f"Worker {worker_id}: Executing task {task['id']} - '{queue_name}' with data={data_model}"
                )
                # Используем asyncio.wait_for для установки тайм-аута
                if timeout:
                    await asyncio.wait_for(func(data=data_model), timeout=timeout)
                else:
                    await func(data=data_model)
            else:
                self.logger.info(
                    f"Worker {worker_id}: Executing task {task['id']} - '{queue_name}' with kwargs={kwargs}"
                )
                # Используем asyncio.wait_for для установки тайм-аута
                if timeout:
                    await asyncio.wait_for(func(**kwargs), timeout=timeout)
                else:
                    await func(**kwargs)

            self.logger.info(
                f"Worker {worker_id}: Task {task['id']} completed successfully."
            )
        except asyncio.TimeoutError:
            self.logger.error(
                f"Worker {worker_id}: Task {task['id']} exceeded timeout of {timeout} seconds."
            )
            if task["retry_count"] < self.retry_limit:
                task["retry_count"] += 1
                try:
                    await self.task_queue.requeue(task, queue_name=self.queue_name)
                    self.logger.info(
                        f"Worker {worker_id}: Requeued task {task['id']} due to timeout (retry {task['retry_count']})"
                    )
                except TaskRequeueError as re:
                    self.logger.error(
                        f"Worker {worker_id}: Failed to requeue task {task['id']}: {re}"
                    )
            else:
                self.logger.error(
                    f"Worker {worker_id}: Task {task['id']} failed after exceeding timeout and {self.retry_limit} retries."
                )
                # Дополнительная обработка ошибки (при необходимости)
        except Exception as e:
            self.logger.exception(
                f"Worker {worker_id}: Error executing task {task['id']}: {e}"
            )
            if task["retry_count"] < self.retry_limit:
                task["retry_count"] += 1
                try:
                    await self.task_queue.requeue(task, queue_name=self.queue_name)
                    self.logger.info(
                        f"Worker {worker_id}: Requeued task {task['id']} (retry {task['retry_count']})"
                    )
                except TaskRequeueError as re:
                    self.logger.error(
                        f"Worker {worker_id}: Failed to requeue task {task['id']}: {re}"
                    )
            else:
                self.logger.error(
                    f"Worker {worker_id}: Task {task['id']} failed after {self.retry_limit} retries."
                )
                # Дополнительная обработка ошибки (при необходимости)
        finally:
            # Всегда вызываем mark_completed
            try:
                await self.task_queue.mark_completed(task["id"], queue_name)
                self.logger.info(
                    f"Worker {worker_id}: Task {task['id']} marked as completed."
                )
            except Exception as e:
                self.logger.exception(
                    f"Worker {worker_id}: Failed to mark task {task['id']} as completed: {e}"
                )


# ============================
# ScheduledTask Class
# ============================


class ScheduledTask:
    """
    Represents a task scheduled to run at fixed intervals or based on cron expressions.
    """

    def __init__(
        self,
        func: Callable,
        interval: Optional[int] = None,
        cron_expression: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize the ScheduledTask.

        Args:
            func (Callable): The function to execute.
            interval (Optional[int], optional): Interval in seconds between executions.
            cron_expression (Optional[str], optional): Cron expression defining the schedule.
            logger (logging.Logger, optional): Logger instance for logging.
        """
        self.func = func
        self.interval = interval
        self.cron_expression = cron_expression
        self.last_run = None
        self.next_run = self._get_next_run()
        self.is_running = False
        self.logger = logger or logging.getLogger("ScheduledTask")

    def _get_next_run(self) -> datetime:
        """
        Calculate the next run time based on the schedule.

        Returns:
            datetime: The next scheduled run time.

        Raises:
            ValueError: If neither interval nor cron_expression is set.
        """
        now = datetime.now()
        if self.interval is not None:
            return now + timedelta(seconds=self.interval)
        elif self.cron_expression is not None:
            cron = croniter(self.cron_expression, now)
            return cron.get_next(datetime)
        else:
            raise ValueError("Either interval or cron_expression must be set")

    async def run(self):
        """
        Execute the scheduled task.
        """
        if self.is_running:
            return
        self.is_running = True
        try:
            await self.func()
        except Exception as e:
            self.logger.exception(f"Error in scheduled task {self.func.__name__}: {e}")
        finally:
            self.last_run = datetime.now()
            self.next_run = self._get_next_run()
            self.is_running = False

    def should_run(self) -> bool:
        """
        Determine if the task should run at the current time.

        Returns:
            bool: True if the task should run, False otherwise.
        """
        return datetime.now() >= self.next_run and not self.is_running


# ============================
# mTask Class
# ============================


class mTask:
    """
    Main class to manage tasks, scheduling, and workers.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        retry_limit: int = 3,
        log_level: int = logging.INFO,
        enable_logging: bool = True,
    ):
        """
        Initialize the mTask manager.

        Args:
            redis_url (str, optional): Redis connection URL.
            retry_limit (int, optional): Maximum number of retry attempts for failed tasks.
            log_level (int, optional): Logging level (e.g., logging.INFO, logging.DEBUG).
            enable_logging (bool, optional): Enable or disable logging.
        """
        self.task_queue = TaskQueue(redis_url=redis_url)
        self.task_registry: Dict[str, Dict[str, Any]] = {}
        self.workers: Dict[str, Worker] = {}
        self.scheduled_tasks: List[ScheduledTask] = []
        self.retry_limit = retry_limit
        self.semaphores: Dict[str, asyncio.Semaphore] = {}
        self.queue_status: Dict[str, str] = {}  # Tracks status of each queue

        # Locks for thread-safe access in synchronous code
        self.task_registry_lock = threading.Lock()
        self.queue_status_lock = threading.Lock()

        # Async locks for use within async code
        self.async_task_registry_lock = asyncio.Lock()
        self.async_queue_status_lock = asyncio.Lock()

        # Set up logging
        self.logger = logging.getLogger("mTask")
        if enable_logging:
            self.logger.setLevel(log_level)
            if not self.logger.handlers:
                handler = logging.StreamHandler()
                formatter = logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
                )
                handler.setFormatter(formatter)
                self.logger.addHandler(handler)
        else:
            self.logger.disabled = True  # Disable logging

        self.logger.debug("Initialized mTask instance.")

        # Initialize the status report scheduled task
        self._initialize_status_report_task()

    def _initialize_status_report_task(self):
        """
        Initialize an internal scheduled task that reports the status of all queues every 5 minutes.
        """

        @self.interval(seconds=300)  # 5 minutes
        async def status_report_task():
            """
            Internal task to report the status of all queues.
            """
            if not self.task_registry:
                self.logger.info("No queues registered.")
                return

            report_data = []
            queue_names = list(self.task_registry.keys())

            for queue_name in queue_names:
                task_info = self.task_registry.get(queue_name, {})
                concurrency = task_info.get("concurrency", 1)
                status = await self._get_queue_status(queue_name)

                # Get the number of tasks in the main queue
                queue_length = await self.task_queue.get_task_count(queue_name)
                processing_length = await self.task_queue.get_processing_task_count(
                    queue_name
                )

                report_data.append(
                    {
                        "Queue Name": queue_name,
                        "Concurrency": concurrency,
                        "Tasks in Queue": queue_length,
                        "Processing Tasks": processing_length,
                        "Status": status,
                    }
                )

            # Format the report as a table
            table = self._format_table(report_data)
            print(
                f"\n\t=== Queue Status Report ===\n\n{table}\n\n\t============================\n"
            )

    def _format_table(self, data: List[Dict[str, Any]]) -> str:
        """
        Format the data into a table string.

        Args:
            data (List[Dict[str, Any]]): List of dictionaries containing queue information.

        Returns:
            str: Formatted table as a string.
        """
        if not data:
            return "No data to display."

        # Get headers
        headers = data[0].keys()
        # Calculate column widths
        column_widths = {header: len(header) for header in headers}
        for row in data:
            for header, value in row.items():
                column_widths[header] = max(column_widths[header], len(str(value)))

        # Create header row
        header_row = " | ".join(
            f"{header:<{column_widths[header]}}" for header in headers
        )
        # Create separator
        separator = "-+-".join("-" * column_widths[header] for header in headers)
        # Create data rows
        data_rows = "\n".join(
            " | ".join(
                f"{str(value):<{column_widths[header]}}"
                for header, value in row.items()
            )
            for row in data
        )

        # Combine all parts
        table = f"{header_row}\n{separator}\n{data_rows}"
        return table

    def agent(
        self,
        queue_name: str = "default",
        concurrency: int = 1,
        timeout: Optional[int] = None,
    ):
        """
        Decorator to define and register a task.

        Args:
            queue_name (str, optional): Redis queue name to enqueue the task.
            concurrency (int, optional): Maximum concurrent executions for the task.
            timeout (int, optional): Maximum execution time for tasks in this queue. Seconds.

        Returns:
            Callable: The decorator.
        """

        def decorator(func: Callable):
            """
            Decorator function to register the task.

            Args:
                func (Callable): The task function to register.

            Returns:
                Callable: The wrapped function.
            """
            # Register the function, concurrency, and timeout in the task_registry
            with self.task_registry_lock:
                self.task_registry[queue_name] = {
                    "func": func,
                    "concurrency": concurrency,
                    "timeout": timeout,  # Сохраняем тайм-аут для задачи
                }
            with self.queue_status_lock:
                self.queue_status[queue_name] = (
                    "Running"  # Initialize status as Running
                )

            @wraps(func)
            async def wrapper(*args, **kwargs):
                """
                Wrapper function to enqueue the task.

                Args:
                    *args: Variable length argument list.
                    **kwargs: Arbitrary keyword arguments.
                """
                # Determine if the function expects a Pydantic model
                sig = inspect.signature(func)
                model = None
                for name, param in sig.parameters.items():
                    if (
                        param.annotation
                        and inspect.isclass(param.annotation)
                        and issubclass(param.annotation, BaseModel)
                    ):
                        model = param.annotation
                        break

                if model:
                    try:
                        data = model(**kwargs).model_dump()
                    except Exception as e:
                        self.logger.error(f"Error parsing data with model {model}: {e}")
                        raise

                    # Enqueue the task without passing concurrency
                    try:
                        task_id = await self.task_queue.enqueue(
                            queue_name=queue_name,
                            kwargs=data,
                        )
                        self.logger.info(
                            f"Task for queue '{queue_name}' enqueued with ID {task_id}"
                        )
                        return task_id
                    except TaskEnqueueError as e:
                        self.logger.error(f"Failed to enqueue task: {e}")
                        raise
                else:
                    # No Pydantic model; pass kwargs directly
                    try:
                        task_id = await self.task_queue.enqueue(
                            queue_name=queue_name,
                            kwargs=kwargs,
                        )
                        self.logger.info(
                            f"Task for queue '{queue_name}' enqueued with ID {task_id}"
                        )
                        return task_id
                    except TaskEnqueueError as e:
                        self.logger.error(f"Failed to enqueue task: {e}")
                        raise

            return wrapper

        return decorator

    def interval(
        self,
        seconds: int = 60,
    ):
        """
        Decorator to schedule a task to run at fixed intervals.

        Args:
            seconds (int, optional): Interval in seconds between executions.

        Returns:
            Callable: The decorator.
        """

        def decorator(func: Callable):
            """
            Decorator function to schedule the task.

            Args:
                func (Callable): The scheduled task function.

            Returns:
                Callable: The original function.
            """
            scheduled_task = ScheduledTask(func, interval=seconds, logger=self.logger)
            self.scheduled_tasks.append(scheduled_task)
            self.logger.debug(
                f"Scheduled interval task '{func.__name__}' every {seconds} seconds."
            )
            return func

        return decorator

    def cron(
        self,
        cron_expression: str,
    ):
        """
        Decorator to schedule a task based on a cron expression.

        Args:
            cron_expression (str): Cron expression defining the schedule.

        Returns:
            Callable: The decorator.
        """

        def decorator(func: Callable):
            """
            Decorator function to schedule the task.

            Args:
                func (Callable): The scheduled task function.

            Returns:
                Callable: The original function.
            """
            scheduled_task = ScheduledTask(
                func, cron_expression=cron_expression, logger=self.logger
            )
            self.scheduled_tasks.append(scheduled_task)
            self.logger.debug(
                f"Scheduled cron task '{func.__name__}' with cron expression '{cron_expression}'."
            )
            return func

        return decorator

    async def add_task(
        self,
        queue_name: str,
        data_model: Any,
    ) -> str:
        """
        Manually add a task to the main queue with Pydantic model data or a dictionary.

        Args:
            queue_name (str): Name of the Redis queue.
            data_model (Any): Pydantic model or dictionary containing task parameters.

        Returns:
            str: Unique identifier of the enqueued task.

        Raises:
            TaskEnqueueError: If enqueueing the task fails.
        """
        if isinstance(data_model, BaseModel):
            data = data_model.model_dump()
        elif isinstance(data_model, dict):
            data = data_model
        else:
            self.logger.error("data_model must be a Pydantic model or a dictionary.")
            raise ValueError("data_model must be a Pydantic model or a dictionary.")

        try:
            task_id = await self.task_queue.enqueue(
                queue_name=queue_name,
                kwargs=data,
            )
            self.logger.info(
                f"Manual task enqueued in queue '{queue_name}' with ID {task_id}"
            )
            return task_id
        except TaskEnqueueError as e:
            self.logger.error(f"Failed to enqueue manual task: {e}")
            raise

    def start_worker(
        self,
        queue_name: str,
        semaphore: Optional[asyncio.Semaphore] = None,
    ):
        """
        Start a worker for a specific queue.

        Args:
            queue_name (str): Name of the Redis queue.
            semaphore (asyncio.Semaphore, optional): Semaphore to limit concurrency.
        """
        if queue_name in self.workers:
            self.logger.warning(f"Worker for queue '{queue_name}' is already running.")
            return
        semaphore = semaphore if semaphore else asyncio.Semaphore(1)
        worker = Worker(
            task_queue=self.task_queue,
            task_registry=self.task_registry,
            async_task_registry_lock=self.async_task_registry_lock,
            retry_limit=self.retry_limit,
            queue_name=queue_name,
            semaphore=semaphore,  # Pass semaphore to worker
            logger=self.logger,  # Pass logger to worker
        )
        self.workers[queue_name] = worker
        asyncio.create_task(worker.start())
        self.logger.debug(
            f"Started worker for queue '{queue_name}' with concurrency {semaphore._value}"
        )

    async def run_scheduled_tasks(self):
        """
        Continuously check and run scheduled tasks.
        """
        while True:
            for task in self.scheduled_tasks:
                if task.should_run():
                    asyncio.create_task(task.run())
            await asyncio.sleep(1)

    async def connect_and_start_workers(self):
        """
        Connect to Redis and start all workers, recovering any tasks from processing queues.
        """
        try:
            # Pass the mTask's logger to TaskQueue
            self.task_queue = TaskQueue(
                redis_url=self.task_queue.redis_url, logger=self.logger
            )
            await self.task_queue.connect()
        except RedisConnectionError as e:
            self.logger.error(f"Cannot start workers without Redis connection: {e}")
            raise

        # Recover tasks from processing queues
        queue_names = list(self.task_registry.keys())

        for queue_name in queue_names:
            await self.task_queue.recover_processing_tasks(queue_name)

        # Create and start a worker for each queue based on concurrency
        for queue_name in queue_names:
            task_info = self.task_registry.get(queue_name, {})
            concurrency = task_info.get("concurrency", 1)
            semaphore = asyncio.Semaphore(concurrency)
            self.semaphores[queue_name] = semaphore  # Save semaphore
            self.start_worker(
                queue_name=queue_name,
                semaphore=semaphore,
            )

        # Start the scheduled tasks runner
        asyncio.create_task(self.run_scheduled_tasks())

    async def run(self):
        """
        Start the TaskQueue, Workers, Scheduled Tasks, and keep the event loop running.
        """
        try:
            await self.connect_and_start_workers()
        except mTaskError as e:
            self.logger.error(f"Failed to start mTask: {e}")
            return

        asyncio.create_task(self.monitor_queue_status())

        self.logger.info("mTask is running. Press Ctrl+C to exit.")

        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("Shutting down mTask...")
            # Stop all workers
            for worker in self.workers.values():
                await worker.stop()
            # Disconnect from Redis
            await self.task_queue.disconnect()
            self.logger.info("mTask has been shut down.")
        except Exception as e:
            self.logger.exception(f"Unexpected error: {e}")
            # Stop all workers
            for worker in self.workers.values():
                await worker.stop()
            # Disconnect from Redis
            await self.task_queue.disconnect()
            self.logger.info("mTask has been shut down due to an error.")

    async def _get_queue_status(self, queue_name: str) -> str:
        """
        Get the status of the queue from Redis.

        Args:
            queue_name (str): Name of the Redis queue.

        Returns:
            str: Status of the queue ("Running" or "Paused").
        """
        status_key = f"queue_status:{queue_name}"
        status = await self.task_queue.redis.get(status_key) or "Running"
        return status

    async def pause_queue(self, queue_name: str, duration: int):
        """
        Pause all workers for a specific queue for a given duration.

        Args:
            queue_name (str): Name of the Redis queue to pause.
            duration (int): Duration in seconds to pause the queue.

        Raises:
            mTaskError: If the queue does not exist or is already paused.
        """
        if queue_name not in self.workers:
            self.logger.error(f"Queue '{queue_name}' not found.")
            raise mTaskError(f"Queue '{queue_name}' not found.")

        status_key = f"queue_status:{queue_name}"
        try:
            current_status = await self.task_queue.redis.get(status_key) or "Running"
            if current_status == "Paused":
                self.logger.warning(f"Queue '{queue_name}' is already paused.")
                return

            # Set status to Paused in Redis with TTL
            await self.task_queue.redis.set(status_key, "Paused", ex=duration)

            # Stop the worker
            await self.workers[queue_name].stop()

            # Remove the worker from self.workers
            del self.workers[queue_name]

            # Move tasks from processing queue back to main queue (to the front)
            processing_queue = f"{queue_name}:processing"
            tasks = await self.task_queue.redis.lrange(processing_queue, 0, -1)
            if tasks:
                # Use LPUSH to add tasks to the front of the main queue
                await self.task_queue.redis.lpush(queue_name, *tasks[::-1])
                # Clear the processing queue
                await self.task_queue.redis.delete(processing_queue)
                self.logger.info(
                    f"Moved {len(tasks)} tasks from processing queue back to main queue '{queue_name}'."
                )

            # Remove the semaphore
            if queue_name in self.semaphores:
                del self.semaphores[queue_name]

            async with self.async_queue_status_lock:
                self.queue_status[queue_name] = "Paused"
            self.logger.info(f"Queue '{queue_name}' paused for {duration} seconds.")
        except Exception as e:
            self.logger.exception(f"Failed to pause queue '{queue_name}': {e}")
            raise mTaskError(f"Failed to pause queue '{queue_name}': {e}") from e

    async def monitor_queue_status(self):
        """
        Periodically checks the status of queues and restarts workers as needed.
        """
        while True:
            queue_names = list(self.task_registry.keys())

            for queue_name in queue_names:
                status_key = f"queue_status:{queue_name}"
                current_status = (
                    await self.task_queue.redis.get(status_key) or "Running"
                )
                previous_status = self.queue_status.get(queue_name, "Running")

                if current_status != previous_status:
                    if current_status == "Paused":
                        async with self.async_queue_status_lock:
                            self.queue_status[queue_name] = "Paused"
                        self.logger.info(f"Queue '{queue_name}' is paused.")
                        # Stop workers
                        if queue_name in self.workers:
                            await self.workers[queue_name].stop()
                            # Remove the worker from self.workers
                            del self.workers[queue_name]
                    elif current_status == "Running" or current_status is None:
                        async with self.async_queue_status_lock:
                            self.queue_status[queue_name] = "Running"
                        self.logger.info(f"Queue '{queue_name}' is resumed.")
                        # Retrieve the concurrency setting
                        async with self.async_task_registry_lock:
                            task_info = self.task_registry.get(queue_name, {})
                            concurrency = task_info.get("concurrency", 1)
                        # Create a semaphore with correct concurrency
                        semaphore = asyncio.Semaphore(concurrency)
                        self.semaphores[queue_name] = semaphore  # Update the semaphore
                        self.start_worker(queue_name, semaphore)
                    else:
                        # Handle other statuses if necessary
                        pass
            await asyncio.sleep(5)  # Check status every 5 seconds
