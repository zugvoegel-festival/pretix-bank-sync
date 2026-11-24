from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pretix_bank_sync', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='transactionmatchsuggestion',
            name='match_type',
            field=models.CharField(
                choices=[
                    ('event_order', 'Event slug + order code'),
                    ('exact_code', 'Exact order code match'),
                    ('fuzzy_code', 'Order code within other words'),
                    ('sender_name', 'Sender name match'),
                    ('amount_only', 'Amount match only'),
                    ('manual', 'Manually assigned'),
                ],
                help_text='Type of match',
                max_length=20,
            ),
        ),
    ]
