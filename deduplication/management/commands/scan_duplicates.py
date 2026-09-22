from django.core.management.base import BaseCommand, CommandError

from core.models import User
from deduplication.services import run_scan


class Command(BaseCommand):
    help = "Scan registered candidate sources and record duplicate candidates."

    def add_arguments(self, parser):
        parser.add_argument(
            '--kind', action='append', dest='kinds', default=None,
            help="Restrict the scan to this source kind; repeatable.",
        )
        parser.add_argument(
            '--user', dest='username', default=None,
            help="Username to attribute the scan to (defaults to the system user).",
        )

    def handle(self, *args, **options):
        username = options.get('username')
        actor = (
            User.objects.filter(username=username).first() if username
            else User.objects.filter(i_user_id=1).first()
        )
        if actor is None:
            raise CommandError("No actor user found; pass --user or seed the system user.")

        counts = run_scan(kinds=options.get('kinds'), actor=actor)
        for kind, count in counts.items():
            self.stdout.write(self.style.SUCCESS(f"{kind}: {count} candidate(s) recorded"))
