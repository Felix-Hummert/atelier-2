"""The HTTP surface, one module per route group.

Registration order is the published document's `paths` key order, so
`create_app` includes these routers in the order they are listed here and a
route stays inside the group it was registered with.
"""
