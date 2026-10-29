FROM container-registry.oracle.com/oci_functions/python:3.12-dev AS build
WORKDIR /function
COPY function/requirements.txt /function/requirements.txt
RUN pip3 install --no-cache-dir --target /python -r requirements.txt

FROM container-registry.oracle.com/oci_functions/python:3.12
WORKDIR /function
COPY --from=build /python /python
COPY onboard_osmh.py osmh_discovery.py osmh_iam.py osmh_runtime.py osmh_tags.py osmh_deployment.py osmh_function_setup.py reconcile_osmh.py /function/
COPY function/func.py /function/func.py
ENV PYTHONPATH=/function:/python PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["/python/bin/fdk", "/function/func.py", "handler"]
