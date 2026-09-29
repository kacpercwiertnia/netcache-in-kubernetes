package main

import (
	"os"

	"evalnet.local/netcache-scheduler/pkg/netcache"
	"k8s.io/component-base/cli"
	"k8s.io/kubernetes/cmd/kube-scheduler/app"
)

func main() {
	command := app.NewSchedulerCommand(
		app.WithPlugin(netcache.Name, netcache.New),
	)
	os.Exit(cli.Run(command))
}
