from django.db import migrations

candidate_rights = [172003, 172004, 172005]
imis_administrator_system = 64


def add_rights(apps, schema_editor):
    role = apps.get_model('core', 'role').objects.filter(is_system=imis_administrator_system).first()
    if role is None:
        return
    role_right = apps.get_model('core', 'roleright')
    for right_id in candidate_rights:
        if not role_right.objects.filter(validity_to__isnull=True, role=role, right_id=right_id).exists():
            role_right.objects.create(role=role, right_id=right_id, audit_user_id=1)


def remove_rights(apps, schema_editor):
    apps.get_model('core', 'roleright').objects.filter(
        role__is_system=imis_administrator_system,
        right_id__in=candidate_rights,
        validity_to__isnull=True
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("deduplication", "0002_duplicate_candidate_and_scan_state"),
    ]

    operations = [
        migrations.RunPython(add_rights, remove_rights),
    ]
