"""API routers, one module per area. main.py includes everything in `all_routers`."""
from . import catalog, dictionary, documents, folders, live, system

all_routers = [system.router, documents.router, folders.router, catalog.router,
               dictionary.router, live.router]
