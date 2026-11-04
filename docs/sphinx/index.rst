Doxtruder Documentation
=======================

**Doxtruder** is a documentation generator that renders infrastructure config
snapshots as Markdown.

Doxtruder tries to solve the issue that if you start documenting your systems,
the information will quickly be outdated with every configuration change.
Doxtruder tries to tackle this by just reading all the available configuration
from any source, converting it into JSON format and then use Jinja2 templates
to convert the JSON into a readable format.

The workflow is: You create a YAML file which describes the APIs you want to
read, you put secrets into an ``.env`` file, then you just run Doxtruder and
the readable files will be updated.

The Jinja2 templates that come with Doxtruder will create MD files, but since
Jinja2 is used, you are free to convert the configuration JSON files into
whatever you consider sensible.

This code was partly written by AI.

Command line usage
------------------

Doxtruder is started via the ``doxtruder`` console script (installed with
``pip install -e .``)::

   doxtruder --config doxtruder.yaml --env .env

.. list-table:: Command line options
   :header-rows: 1
   :widths: 25 75

   * - Option
     - Description
   * - ``-c``, ``--config``
     - Configuration file (defaults to ``doxtruder.yaml``).
   * - ``-v``, ``--loglevel``
     - Logging verbosity: 1 critical, 2 error, 3 warning, 4 info, 5 debug.
   * - ``-e``, ``--env``
     - Environment file with ``<KEY>=<VALUE>`` definitions (defaults to
       ``.env``). ``${<KEY>}`` placeholders in the config are replaced by
       ``<VALUE>``.
   * - ``-s``, ``--store``
     - Store the JSON files in the output path. A folder with the timestamp
       will be created.
   * - ``-t``, ``--templates``
     - Set the path to the templates (recommended: use a copy of the original
       templates).
   * - ``-i``, ``--ssl-cert-ignore``
     - Ignore SSL certificate errors. Server certificates are not verified
       (useful for hosts with self-signed certificates). Off by default:
       certificates are validated.

.. toctree::
   :maxdepth: 2
   :caption: Contents

   api

Indices and tables
------------------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
