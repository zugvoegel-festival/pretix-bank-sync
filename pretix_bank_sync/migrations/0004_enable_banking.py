# Generated manually — switch provider from GoCardless to Enable Banking

from django.db import migrations, models
import django.db.models.deletion
import pretix.base.models.base


class Migration(migrations.Migration):

    dependencies = [
        ('pretix_bank_sync', '0003_rate_limiting'),
    ]

    operations = [
        migrations.AddField(
            model_name='bankconnection',
            name='session_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                help_text='Enable Banking session ID',
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='bankconnection',
            name='aspsp_name',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Bank (ASPSP) name',
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='bankconnection',
            name='aspsp_country',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Bank country code',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='bankconnection',
            name='auth_link',
            field=models.URLField(
                blank=True,
                default='',
                help_text='Pending authorization URL',
                max_length=500,
            ),
        ),
        migrations.AlterField(
            model_name='bankconnection',
            name='requisition_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='Legacy GoCardless requisition ID (unused)',
                max_length=255,
                null=True,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name='bankconnection',
            name='consent_id',
            field=models.CharField(
                blank=True,
                help_text='Legacy consent ID (unused for Enable Banking)',
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name='bankconnection',
            name='account_ids',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Cached account UIDs (legacy / fallback)',
            ),
        ),
        migrations.AlterField(
            model_name='banktransaction',
            name='transaction_id',
            field=models.CharField(
                db_index=True,
                help_text='Bank transaction ID',
                max_length=255,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name='banktransaction',
            name='account_id',
            field=models.CharField(
                db_index=True,
                help_text='Bank account UID',
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name='banktransaction',
            name='raw_data',
            field=models.JSONField(
                default=dict,
                help_text='Raw transaction data from the bank provider (IBANs redacted)',
            ),
        ),
        migrations.CreateModel(
            name='BankAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('account_uid', models.CharField(max_length=255)),
                ('account_name', models.CharField(blank=True, default='', max_length=255)),
                ('iban', models.CharField(blank=True, default='', max_length=34)),
                ('currency', models.CharField(default='EUR', max_length=10)),
                ('is_active', models.BooleanField(default=True)),
                ('last_fetched', models.DateTimeField(blank=True, null=True)),
                ('last_fetch_date', models.DateField(blank=True, null=True)),
                ('connection', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='accounts',
                    to='pretix_bank_sync.bankconnection',
                )),
            ],
            options={
                'verbose_name': 'Bank Account',
                'verbose_name_plural': 'Bank Accounts',
                'ordering': ('account_name', 'iban'),
                'unique_together': {('connection', 'account_uid')},
            },
            bases=(models.Model, pretix.base.models.base.LoggingMixin),
        ),
    ]
