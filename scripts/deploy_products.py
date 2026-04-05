#!/usr/bin/env python3
"""
deploy_products.py (FIXED VERSION)
──────────────────────────────────
Improved debugging + proper error visibility
"""

import boto3
import json
import os
import sys
import traceback
import logging
from pathlib import Path

# ─── logging setup ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ─── config ───────────────────────────────────────────────────────
def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"❌ Missing required environment variable: {name}")
    return value

TEMPLATE_BUCKET = get_env("TEMPLATE_BUCKET")
PORTFOLIO_ID    = get_env("PORTFOLIO_ID")
LAUNCH_ROLE_ARN = get_env("LAUNCH_ROLE_ARN")
ACCOUNT_ID      = get_env("AWS_ACCOUNT_ID")

REGION = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

logging.info(f"Using region: {REGION}")

sc  = boto3.client("servicecatalog", region_name=REGION)
cfn = boto3.client("cloudformation",  region_name=REGION)

# ─── helpers ──────────────────────────────────────────────────────

def s3_template_url(product_name: str, prdouct_version: str) -> str:
    return f"https://{TEMPLATE_BUCKET}.s3.{REGION}.amazonaws.com/products/{product_name}/{prdouct_version}/template.yaml"


def find_product_id(product_name: str):
    paginator = sc.get_paginator("search_products_as_admin")
    for page in paginator.paginate(Filters={"FullTextSearch": [product_name]}):
        for detail in page.get("ProductViewDetails", []):
            if detail["ProductViewSummary"]["Name"] == product_name:
                return detail["ProductViewSummary"]["ProductId"]
    return None


def list_artifact_names(product_id: str):
    resp = sc.list_provisioning_artifacts(ProductId=product_id)
    return [a["Name"] for a in resp["ProvisioningArtifactDetails"]]


def find_provisioned_product(pp_name: str):
    """Return the provisioned product if exists"""
    try:
        resp = sc.search_provisioned_products(
            Filters={"SearchQuery": [f"name:{pp_name}"]}
        )

        for pp in resp.get("ProvisionedProducts", []):
            if pp["Name"] == pp_name:
                return pp

        return None

    except Exception:
        import traceback
        traceback.print_exc()
        raise

def aws_call(fn, *args, **kwargs):
    """Wrapper to log AWS errors properly"""
    try:
        return fn(*args, **kwargs)
    except Exception:
        logging.error("🔥 AWS CALL FAILED")
        traceback.print_exc()
        raise


# ─── main logic ───────────────────────────────────────────────────

def deploy_product(config_path: Path,provision_product_name):
    logging.info(f"Processing config: {config_path}")

    config = json.loads(config_path.read_text())

    product_name   = config["productName"]
    description    = config.get("description", "")
    auto_provision = config.get("autoProvision", True)
    parameters     = config.get("provisioningParameters", [])
    product_version = config.get("productVersion", "latest")
    support_description = config.get("supportDescription", "Default")
    support_email = config.get("supportEmail", "Default")
    support_link = config.get("supportLink", "Default")

    version_name = f"{product_version}"
    template_url = s3_template_url(product_name, version_name)
    pp_name      = f"{provision_product_name}"

    logging.info(f"Product: {product_name}")
    logging.info(f"Template: {template_url}")

    # ── 1. Create  / Update Product ───────────────────────────────
    product_id = find_product_id(product_name)

    if product_id is None:
        logging.info("Creating new product...")

        resp = aws_call(
            sc.create_product,
            Name=product_name,
            Description=description,
            ProductType="CLOUD_FORMATION_TEMPLATE",
            Owner=f"account/{ACCOUNT_ID}",
            SupportDescription=support_description,
            SupportEmail=support_email,
            SupportUrl=support_link,
            ProvisioningArtifactParameters={
                "Name": version_name,
                "Description": f"Auto-deployed from git {product_version}",
                "Type": "CLOUD_FORMATION_TEMPLATE",
                "Info": {"LoadTemplateFromURL": template_url},
            },
        )

        product_id = resp["ProductViewDetail"]["ProductViewSummary"]["ProductId"]
        artifact_id = resp["ProvisioningArtifactDetail"]["Id"]

        logging.info(f"Product created: {product_id}")

        aws_call(
            sc.associate_product_with_portfolio,
            ProductId=product_id,
            PortfolioId=PORTFOLIO_ID,
        )

        aws_call(
            sc.create_constraint,
            PortfolioId=PORTFOLIO_ID,
            ProductId=product_id,
            Type="LAUNCH",
            Parameters=json.dumps({"RoleArn": LAUNCH_ROLE_ARN}),
        )

    else:
        logging.info("Product exists, checking versions...")
        existing_versions = list_artifact_names(product_id)

        if version_name in existing_versions:
            logging.info("Version already exists, reusing artifact")

            arts = sc.list_provisioning_artifacts(ProductId=product_id)
            artifact_id = next(
                a["Id"] for a in arts["ProvisioningArtifactDetails"]
                if a["Name"] == version_name
            )
        else:
            logging.info("Creating new version...")

            resp = aws_call(
                sc.create_provisioning_artifact,
                ProductId=product_id,
                Parameters={
                    "Name": version_name,
                    "Description": f"Auto-deployed from git {product_version}",
                    "Type": "CLOUD_FORMATION_TEMPLATE",
                    "Info": {"LoadTemplateFromURL": template_url},
                },
            )
            artifact_id = resp["ProvisioningArtifactDetail"]["Id"]

    # ── 2. Provision / Update ───────────────────────────────────
    if not auto_provision:
        logging.info("Skipping provisioning (autoProvision=false)")
        return

    pp = find_provisioned_product(pp_name)

    if pp is None:
        logging.info(f"Provisioning new product: {pp_name}")

        aws_call(
            sc.provision_product,
            ProductId=product_id,
            ProvisioningArtifactId=artifact_id,
            ProvisionedProductName=pp_name,
            ProvisioningParameters=parameters,
        )

    else:
        status = pp["Status"]

        if status in ("UNDER_CHANGE", "PLAN_IN_PROGRESS"):
            logging.warning(f"Already in progress: {status}")
            return

        logging.info(f"Updating product: {pp_name}")

        aws_call(
            sc.update_provisioned_product,
            ProvisionedProductName=pp_name,
            ProductId=product_id,
            ProvisioningArtifactId=artifact_id,
            ProvisioningParameters=parameters,
        )


def main():
    products_dir = Path("products")
    configs = sorted(products_dir.glob("*/product-config.json"))

    if not configs:
        logging.warning("No product-config.json found")
        sys.exit(0)

    errors = []

    for cfg in configs:
        try:
            deploy_product(cfg,cfg.parent.name)
        except Exception as e:
            logging.error(f"❌ Failed for {cfg.parent.name}")
            traceback.print_exc()
            errors.append((cfg.parent.name, str(e)))

            # 🔥 FAIL FAST (uncomment if needed)
            # sys.exit(1)

    logging.info("═" * 50)
    logging.info(f"Success: {len(configs) - len(errors)}/{len(configs)}")

    if errors:
        for name, err in errors:
            logging.error(f"{name}: {err}")
        sys.exit(1)
    else:
        logging.info("All products deployed successfully ✅")


if __name__ == "__main__":
    main()