from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0005_tenantopsmodechangerequest"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE tenants_tenantaccount
                ADD COLUMN provisioned_by_apex tinyint(1) NOT NULL DEFAULT 0;
            """,
            reverse_sql="""
                ALTER TABLE tenants_tenantaccount
                DROP COLUMN provisioned_by_apex;
            """,
        ),
        migrations.RunSQL(
            sql="""
                UPDATE tenants_tenantaccount
                SET provisioned_by_apex = 1
                WHERE is_illustration = 1;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="""
                UPDATE tenants_tenantaccount
                SET provisioned_by_apex = 1
                WHERE is_illustration = 0
                  AND free_trial_ends_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM tenants_tenantpaymentsubmission p
                    WHERE p.clinic_tin = tenants_tenantaccount.clinic_tin
                      AND p.payment_kind = 'setup'
                  );
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
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
