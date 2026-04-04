#!/usr/bin/env python3
"""
deploy_products.py
──────────────────
Reads every products/<name>/product-config.json, syncs it to Service Catalog,
then provisions (or updates) a running stack instance.

Environment variables (injected by CodeBuild):
  TEMPLATE_BUCKET   – S3 bucket where templates were uploaded
  PORTFOLIO_ID      – Service Catalog portfolio ID
  LAUNCH_ROLE_ARN   – IAM role SC uses to provision stacks
  AWS_REGION_NAME   – e.g. ap-south-1
  AWS_ACCOUNT_ID    – 12-digit account ID
"""

import boto3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ─── config ────────────────────────────────────────────────────────────────
TEMPLATE_BUCKET  = os.environ["TEMPLATE_BUCKET"]
PORTFOLIO_ID     = os.environ["PORTFOLIO_ID"]
LAUNCH_ROLE_ARN  = os.environ["LAUNCH_ROLE_ARN"]
REGION           = os.environ.get("AWS_REGION_NAME", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
ACCOUNT_ID       = os.environ["AWS_ACCOUNT_ID"]

sc  = boto3.client("servicecatalog", region_name=REGION)
cfn = boto3.client("cloudformation",  region_name=REGION)

# ─── helpers ────────────────────────────────────────────────────────────────

def git_sha() -> str:
    # CodeBuild provides this automatically — no git needed
    sha = os.environ.get("CODEBUILD_RESOLVED_SOURCE_VERSION", "")
    if sha:
        return sha[:8]   # use first 8 chars
    return os.environ.get("CODEBUILD_BUILD_NUMBER", "manual")


def s3_template_url(product_name: str, sha: str) -> str:
    return (f"https://{TEMPLATE_BUCKET}.s3.{REGION}.amazonaws.com/"
            f"products/{product_name}/{sha}/template.yaml")


def find_product_id(product_name: str) -> str | None:
    """Return product ID if it already exists, else None."""
    paginator = sc.get_paginator("search_products_as_admin")
    for page in paginator.paginate(Filters={"FullTextSearch": [product_name]}):
        for detail in page.get("ProductViewDetails", []):
            if detail["ProductViewSummary"]["Name"] == product_name:
                return detail["ProductViewSummary"]["ProductId"]
    return None


def list_artifact_names(product_id: str) -> list[str]:
    resp = sc.list_provisioning_artifacts(ProductId=product_id)
    return [a["Name"] for a in resp["ProvisioningArtifactDetails"]]


def find_provisioned_product(pp_name: str) -> dict | None:
    """Return the first provisioned product matching the name, or None."""
    paginator = sc.get_paginator("search_provisioned_products")
    for page in paginator.paginate(
        Filters={"SearchQuery": [f"name:{pp_name}"]}
    ):
        for pp in page.get("ProvisionedProducts", []):
            if pp["Name"] == pp_name:
                return pp
    return None


# ─── main logic ─────────────────────────────────────────────────────────────

def deploy_product(config_path: Path) -> None:
    config = json.loads(config_path.read_text())
    product_name  = config["productName"]
    description   = config.get("description", "")
    auto_provision = config.get("autoProvision", True)
    parameters    = config.get("provisioningParameters", [])
    # e.g. [{"Key": "BucketName", "Value": "my-bucket"}]

    sha           = git_sha()
    version_name  = f"v-{sha}"
    template_url  = s3_template_url(product_name, sha)
    pp_name       = f"{product_name}-live"

    print(f"\n{'─'*60}")
    print(f"Product  : {product_name}")
    print(f"Version  : {version_name}")
    print(f"Template : {template_url}")

    # ── 1. Create or update the product ────────────────────────────────────
    product_id = find_product_id(product_name)

    if product_id is None:
        print("→ Creating new product …")
        resp = sc.create_product(
            Name=product_name,
            Description=description,
            ProductType="CLOUD_FORMATION_TEMPLATE",
            Owner=f"account/{ACCOUNT_ID}",
            ProvisioningArtifactParameters={
                "Name":        version_name,
                "Description": f"Auto-deployed from git {sha}",
                "Type":        "CLOUD_FORMATION_TEMPLATE",
                "Info":        {"LoadTemplateFromURL": template_url},
            },
        )
        product_id = resp["ProductViewDetail"]["ProductViewSummary"]["ProductId"]
        artifact_id = resp["ProvisioningArtifactDetail"]["Id"]
        print(f"  ✓ Product created: {product_id}")

        # Associate with portfolio
        sc.associate_product_with_portfolio(
            ProductId=product_id,
            PortfolioId=PORTFOLIO_ID,
        )
        print(f"  ✓ Associated with portfolio {PORTFOLIO_ID}")

        # Set launch constraint so SC can deploy stacks
        sc.create_constraint(
            PortfolioId=PORTFOLIO_ID,
            ProductId=product_id,
            Type="LAUNCH",
            Parameters=json.dumps({"RoleArn": LAUNCH_ROLE_ARN}),
        )
        print(f"  ✓ Launch constraint set → {LAUNCH_ROLE_ARN}")

    else:
        existing_versions = list_artifact_names(product_id)
        if version_name in existing_versions:
            print(f"  ↩ Version {version_name} already exists, skipping artifact creation")
            # Grab latest artifact ID for provisioning
            arts = sc.list_provisioning_artifacts(ProductId=product_id)
            artifact_id = next(
                a["Id"] for a in arts["ProvisioningArtifactDetails"]
                if a["Name"] == version_name
            )
        else:
            print(f"→ Adding new version to existing product …")
            resp = sc.create_provisioning_artifact(
                ProductId=product_id,
                Parameters={
                    "Name":        version_name,
                    "Description": f"Auto-deployed from git {sha}",
                    "Type":        "CLOUD_FORMATION_TEMPLATE",
                    "Info":        {"LoadTemplateFromURL": template_url},
                },
            )
            artifact_id = resp["ProvisioningArtifactDetail"]["Id"]
            print(f"  ✓ New artifact: {artifact_id}")

    # ── 2. Provision (or update) ────────────────────────────────────────────
    if not auto_provision:
        print("  ↩ autoProvision=false — skipping provisioning")
        return

    pp = find_provisioned_product(pp_name)

    if pp is None:
        print(f"→ Provisioning new product instance: {pp_name} …")
        sc.provision_product(
            ProductId=product_id,
            ProvisioningArtifactId=artifact_id,
            ProvisionedProductName=pp_name,
            ProvisioningParameters=parameters,
            Tags=[
                {"Key": "ManagedBy",  "Value": "CodePipeline"},
                {"Key": "GitSha",     "Value": sha},
                {"Key": "Product",    "Value": product_name},
            ],
        )
        print(f"  ✓ Provisioning started")

    else:
        status = pp["Status"]
        if status in ("UNDER_CHANGE", "PLAN_IN_PROGRESS"):
            print(f"  ↩ Already in progress (status={status}), skipping update")
            return

        print(f"→ Updating provisioned product {pp_name} to {version_name} …")
        sc.update_provisioned_product(
            ProvisionedProductName=pp_name,
            ProductId=product_id,
            ProvisioningArtifactId=artifact_id,
            ProvisioningParameters=parameters,
            Tags=[
                {"Key": "GitSha", "Value": sha},
            ],
        )
        print(f"  ✓ Update started")


def main():
    products_dir = Path("products")
    configs = sorted(products_dir.glob("*/product-config.json"))

    if not configs:
        print("No product-config.json files found under products/. Nothing to deploy.")
        sys.exit(0)

    errors = []
    for cfg in configs:
        try:
            deploy_product(cfg)
        except Exception as exc:
            print(f"\n✗ ERROR deploying {cfg.parent.name}: {exc}")
            errors.append((cfg.parent.name, str(exc)))

    print(f"\n{'═'*60}")
    print(f"Deployed {len(configs) - len(errors)}/{len(configs)} products successfully.")
    if errors:
        for name, err in errors:
            print(f"  ✗ {name}: {err}")
        sys.exit(1)
    else:
        print("All products deployed ✓")


if __name__ == "__main__":
    main()