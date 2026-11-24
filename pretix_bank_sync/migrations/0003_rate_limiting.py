# Rate-limit tracking fields for GoCardless bank sync

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pretix_bank_sync', '0002_match_types'),
    ]

    operations = [
        migrations.AddField(
            model_name='bankconnection',
            name='account_ids',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Cached GoCardless account IDs for this requisition (avoids details API calls)',
            ),
        ),
        migrations.AddField(
            model_name='bankconnection',
            name='last_sync_attempt',
            field=models.DateTimeField(
                blank=True,
                help_text='Last sync attempt (used to space periodic syncs and prevent overlap)',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='bankconnection',
            name='rate_limited_until',
            field=models.DateTimeField(
                blank=True,
                help_text='Do not sync until this time after a GoCardless HTTP 429',
                null=True,
            ),
        ),
    ]
