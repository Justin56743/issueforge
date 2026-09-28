import asyncio
from typing import AsyncGenerator, Callable, Dict, List, Optional
from issueforge.core.models import EventType, TaskEvent, TaskStatus


class EventBus:
    """Pub/Sub event bus for live streaming logs to Web Dashboard and Telegram."""

    def __init__(self):
        self._subscribers: List[asyncio.Queue[TaskEvent]] = []
        self._task_subscribers: Dict[str, List[asyncio.Queue[TaskEvent]]] = {}
        self._status_listeners: List[Callable] = []

    def subscribe(self, task_id: Optional[str] = None) -> asyncio.Queue[TaskEvent]:
        queue: asyncio.Queue[TaskEvent] = asyncio.Queue()
        if task_id:
            if task_id not in self._task_subscribers:
                self._task_subscribers[task_id] = []
            self._task_subscribers[task_id].append(queue)
        else:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[TaskEvent], task_id: Optional[str] = None) -> None:
        if task_id and task_id in self._task_subscribers:
            if queue in self._task_subscribers[task_id]:
                self._task_subscribers[task_id].remove(queue)
        elif queue in self._subscribers:
            self._subscribers.remove(queue)

    async def publish(self, event: TaskEvent) -> None:
        # Broadcast to global subscribers
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except Exception:
                pass

        # Broadcast to specific task subscribers
        if event.task_id in self._task_subscribers:
            for queue in list(self._task_subscribers[event.task_id]):
                try:
                    queue.put_nowait(event)
                except Exception:
                    pass

    async def emit_log(
        self,
        task_id: str,
        message: str,
        role: Optional[str] = None,
        event_type: EventType = EventType.LOG,
        data: Optional[dict] = None,
        run_id: Optional[str] = None
    ) -> None:
        event = TaskEvent(
            task_id=task_id,
            run_id=run_id,
            event_type=event_type,
            role=role,
            message=message,
            data=data
        )

        # Persist event to database so historical logs and Jetson sandbox execution streams survive reloads
        try:
            from issueforge.core.database import save_event
            await save_event(event)
        except Exception:
            pass

        await self.publish(event)


event_bus = EventBus()
