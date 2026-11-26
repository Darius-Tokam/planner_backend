"""Durable cancellation and repeated erasure of DBOS roots and SDK children."""
from datetime import timedelta

from django.db.models.signals import pre_delete
from django.utils import timezone
from core.models import AgentInvocation, AgentWorkflowPurge


def enqueue(sender, instance, **kwargs):
    AgentWorkflowPurge.objects.get_or_create(workflow_id=f'planner-v3-{instance.pk}')


def connect():
    pre_delete.connect(enqueue, sender=AgentInvocation, dispatch_uid='runtime-purge-invocation')


def drain(limit=20):
    from dbos import DBOS
    for item in AgentWorkflowPurge.objects.filter(next_attempt_at__lte=timezone.now()).order_by('next_attempt_at')[:limit]:
        try:
            # SDK child IDs use the root ID prefix. Include detached late children
            # even if a previous pass already erased the root relationship.
            found = DBOS.list_workflows(workflow_id_prefix=item.workflow_id,
                                       load_input=False, load_output=False)
            ids = list({item.workflow_id, *(row.workflow_id for row in found)})
            DBOS.cancel_workflows(ids, cancel_children=True)
            DBOS.delete_workflows(ids, delete_children=True)
            item.last_error = ''
        except Exception as exc:
            item.last_error = type(exc).__name__[:100]
        item.attempts += 1
        # Keep the UUID-only tombstone: an in-flight process can checkpoint late.
        delay = 30 if item.last_error or item.attempts < 5 else 3600
        item.next_attempt_at = timezone.now() + timedelta(seconds=delay)
        item.save(update_fields=['attempts', 'last_error', 'next_attempt_at'])
