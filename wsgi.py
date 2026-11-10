"""Entry point for online hosting (PythonAnywhere, or any WSGI server such as gunicorn).

PythonAnywhere: in the Web tab, open the "WSGI configuration file" and replace
everything in it with the 5 lines below (change YOURNAME to your PythonAnywhere username):

    import os, sys
    os.environ["LASER_HTTPS"] = "1"
    os.environ["LASER_TZ"] = "Asia/Kolkata"
    sys.path.insert(0, "/home/YOURNAME/laserentry")
    from app import app as application
"""
from app import app as application  # noqa: F401
