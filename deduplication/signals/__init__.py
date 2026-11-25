from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal
from deduplication.services import (
    on_deduplication_task_complete_service_handler,
    on_payment_benefit_deduplication_task_complete_service_handler,
    on_deduplication_task_resolve_check,
    on_deduplication_task_complete_check,
)


def bind_service_signals():
    bind_service_signal(
        'task_service.resolve_task',
        on_deduplication_task_resolve_check,
        bind_type=ServiceSignalBindType.BEFORE
    )
    bind_service_signal(
        'task_service.complete_task',
        on_deduplication_task_complete_check,
        bind_type=ServiceSignalBindType.BEFORE
    )
    bind_service_signal(
        'task_service.complete_task',
        on_deduplication_task_complete_service_handler,
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.complete_task',
        on_payment_benefit_deduplication_task_complete_service_handler,
        bind_type=ServiceSignalBindType.AFTER
    )
