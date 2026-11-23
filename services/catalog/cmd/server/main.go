package main

import (
	"os"

	"github.com/MIERAI/api-factory/runtime/server"

	"github.com/MIERAI/storefront/services/catalog/internal/migrations"
)

func main() {
	// `/server migrate [up|status]`: the schema migrations (the Deployment's init container), then exit.
	if len(os.Args) > 1 && os.Args[1] == "migrate" {
		os.Exit(migrations.Main(os.Args[2:], os.Stdout))
	}
	server.Run(services)
}
