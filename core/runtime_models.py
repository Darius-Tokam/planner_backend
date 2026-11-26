"""Durable chat state. Domain effects and operation receipts share one database."""
import uuid

from django.conf import settings
from django.db import models


class AgentRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    conversation = models.ForeignKey('core.Conversation', on_delete=models.CASCADE, related_name='agent_runs')
    payload_hash = models.CharField(max_length=64)
    prompt = models.TextField(blank=True)
    context = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default='queued')
    next_seq = models.PositiveIntegerField(default=0)
    messages = models.JSONField(default=list)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['conversation', 'created_at'])]


class AgentInvocation(models.Model):
    """Immutable input for one workflow, including a resumed tool result."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name='invocations')
    number = models.PositiveIntegerField()
    inputs = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default='queued')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'number'], name='runtime_invocation_number')]


class AgentOperation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name='operations')
    tool_call_id = models.CharField(max_length=200)
    tool = models.CharField(max_length=100)
    arguments_hash = models.CharField(max_length=64)
    arguments = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'tool_call_id'], name='runtime_operation_identity')]


class AgentInteraction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name='interactions')
    tool_call_id = models.CharField(max_length=200)
    kind = models.CharField(max_length=24)
    question = models.JSONField(default=dict)
    prepared = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default='pending')
    answer = models.JSONField(default=dict)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'tool_call_id'], name='runtime_interaction_call')]


class AgentEvent(models.Model):
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name='events')
    seq = models.PositiveIntegerField()
    key = models.CharField(max_length=240)
    kind = models.CharField(max_length=32)
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['seq']
        constraints = [
            models.UniqueConstraint(fields=['run', 'seq'], name='runtime_event_sequence'),
            models.UniqueConstraint(fields=['run', 'key'], name='runtime_event_identity'),
        ]


class AgentModelAttempt(models.Model):
    """One real provider attempt, not one DBOS replay of a saved result."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invocation = models.ForeignKey(AgentInvocation, on_delete=models.CASCADE, related_name='model_attempts')
    request_key = models.CharField(max_length=200)
    status = models.CharField(max_length=24, default='started')
    usage = models.JSONField(default=dict)
    response = models.JSONField(default=list)
    reservation_id = models.BigIntegerField(null=True)
    elapsed_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)


class AgentDelivery(models.Model):
    """Transactional outbox. An unknown provider outcome is never resent blindly."""
    operation = models.OneToOneField(AgentOperation, on_delete=models.CASCADE, primary_key=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default='pending')
    result = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)
