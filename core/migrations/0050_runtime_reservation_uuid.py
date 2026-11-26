from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0049_agent_runtime')]
    operations = [
        migrations.RemoveField(model_name='agentmodelattempt', name='reservation_id'),
        migrations.AddField(model_name='agentmodelattempt', name='reservation_id', field=models.UUIDField(null=True)),
    ]
