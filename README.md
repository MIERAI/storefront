# storefront

> Part of the [GitOps Platform Blueprint](https://github.com/MIERAI/gitops-platform-blueprint)

A monorepo with two services, `services/web` (the page) and `services/api` (products), each released on its own
(`release-web`, `release-api`: only changes under its directory) and promoted on its own through three environments,
dev → staging → prod (manifests: `storefront/` in gitops-manifests). The third reference shape of the platform, next to
sample-app (one service, push) and api-factory (design PR → generated code).
