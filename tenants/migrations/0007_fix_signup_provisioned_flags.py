from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0006_tenantaccount_provisioned_by_apex"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                UPDATE tenants_tenantaccount
                SET provisioned_by_apex = 0
                WHERE free_trial_ends_at IS NOT NULL
                  AND setup_fee_approved = 0
                  AND is_illustration = 0;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
