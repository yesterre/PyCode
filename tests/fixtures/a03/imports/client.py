import pkg
import pkg.api as api
from pkg import public_serve
from pkg.lib import Service as Constructor


def run():
    pkg.public_serve()
    api.serve()
    public_serve()
    Constructor()
