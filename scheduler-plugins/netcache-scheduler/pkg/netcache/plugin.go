package netcache

import (
	"context"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"

	v1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/json"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
	"k8s.io/kubernetes/pkg/scheduler/framework"
)

const (
	Name = "NetCacheAware"

	annotationEnabled          = "cache.evalnet.local/netcache-placement"
	annotationRequestedEntries = "cache.evalnet.local/requested-entries"
	annotationRequestedSlots   = "cache.evalnet.local/requested-slots"
	annotationSelectedSwitch   = "cache.evalnet.local/selected-switch"
	annotationSelectedWorker   = "cache.evalnet.local/selected-worker"
	annotationSelectedHops     = "cache.evalnet.local/selected-hop-count"
	annotationSelectedPathCost = "cache.evalnet.local/selected-path-cost"
	annotationSelectedControl  = "cache.evalnet.local/selected-switch-control-url"

	defaultCapacityWeight = 35.0
	defaultWorkerWeight   = 10.0
	defaultDeviceWeight   = 35.0
	defaultHopWeight      = 20.0
)

var (
	switchGVR = schema.GroupVersionResource{
		Group:    "cache.evalnet.local",
		Version:  "v1alpha1",
		Resource: "netcacheswitches",
	}
	deviceGVR = schema.GroupVersionResource{
		Group:    "cache.evalnet.local",
		Version:  "v1alpha1",
		Resource: "innetworkdevices",
	}
	reachabilityGVR = schema.GroupVersionResource{
		Group:    "cache.evalnet.local",
		Version:  "v1alpha1",
		Resource: "netcachereachabilities",
	}
)

type Plugin struct {
	handle        framework.Handle
	dynamicClient dynamic.Interface
}

type cycleState struct {
	requiredEntries int64
	requiredSlots   int64
	switches        map[string]*switchInfo
	candidates      map[string][]reachabilityCandidate
	selectedByNode  map[string]*switchInfo
	reservedSwitch  string
	reservedEntries int64
	reservedSlots   int64
}

type switchInfo struct {
	name             string
	switchName       string
	attachedWorker   string
	selectedWorker   string
	phase            string
	controlURL       string
	capacityEntries  int64
	capacitySlots    int64
	usedEntries      int64
	freeEntries      int64
	usedSlots        int64
	freeSlots        int64
	hops             int64
	pathCost         int64
	deviceReady      bool
	estimatedCPULoad float64
}

type reachabilityCandidate struct {
	switchName string
	hops       int64
	pathCost   int64
}

type scoreWeights struct {
	name     string
	capacity float64
	worker   float64
	device   float64
	hop      float64
}

func (s *cycleState) Clone() framework.StateData {
	copyState := *s
	copyState.switches = map[string]*switchInfo{}
	for key, value := range s.switches {
		item := *value
		copyState.switches[key] = &item
	}
	copyState.candidates = map[string][]reachabilityCandidate{}
	for key, value := range s.candidates {
		copyState.candidates[key] = append([]reachabilityCandidate{}, value...)
	}
	copyState.selectedByNode = map[string]*switchInfo{}
	for key, value := range s.selectedByNode {
		item := *value
		copyState.selectedByNode[key] = &item
	}
	return &copyState
}

func New(ctx context.Context, _ runtime.Object, handle framework.Handle) (framework.Plugin, error) {
	config, err := rest.InClusterConfig()
	if err != nil {
		return nil, fmt.Errorf("load in-cluster config: %w", err)
	}
	dynamicClient, err := dynamic.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("create dynamic client: %w", err)
	}
	return &Plugin{handle: handle, dynamicClient: dynamicClient}, nil
}

func (p *Plugin) Name() string {
	return Name
}

func (p *Plugin) PreFilter(ctx context.Context, state *framework.CycleState, pod *v1.Pod) (*framework.PreFilterResult, *framework.Status) {
	if !isNetCachePlacementPod(pod) {
		state.Write(Name, &cycleState{
			switches:       map[string]*switchInfo{},
			candidates:     map[string][]reachabilityCandidate{},
			selectedByNode: map[string]*switchInfo{},
		})
		return nil, nil
	}

	requiredEntries, err := intAnnotation(pod, annotationRequestedEntries, 1)
	if err != nil {
		return nil, framework.NewStatus(framework.UnschedulableAndUnresolvable, err.Error())
	}
	requiredSlots, err := intAnnotation(pod, annotationRequestedSlots, requiredEntries)
	if err != nil {
		return nil, framework.NewStatus(framework.UnschedulableAndUnresolvable, err.Error())
	}

	switches, err := p.loadSwitches(ctx)
	if err != nil {
		return nil, framework.AsStatus(err)
	}
	devices, err := p.loadDevices(ctx)
	if err != nil {
		return nil, framework.AsStatus(err)
	}
	for _, item := range switches {
		if device, ok := devices[item.name]; ok {
			item.deviceReady = device.deviceReady
			item.estimatedCPULoad = device.estimatedCPULoad
		}
	}
	candidates, err := p.loadReachabilities(ctx)
	if err != nil {
		return nil, framework.AsStatus(err)
	}
	if len(candidates) == 0 {
		candidates = fallbackReachability(switches)
	}

	state.Write(Name, &cycleState{
		requiredEntries: requiredEntries,
		requiredSlots:   requiredSlots,
		switches:        switches,
		candidates:      candidates,
		selectedByNode:  map[string]*switchInfo{},
	})
	return nil, nil
}

func (p *Plugin) PreFilterExtensions() framework.PreFilterExtensions {
	return nil
}

func (p *Plugin) Filter(ctx context.Context, state *framework.CycleState, pod *v1.Pod, nodeInfo *framework.NodeInfo) *framework.Status {
	if !isNetCachePlacementPod(pod) {
		return nil
	}
	data, status := readState(state)
	if !status.IsSuccess() {
		return status
	}
	node := nodeInfo.Node()
	if node == nil {
		return framework.NewStatus(framework.Unschedulable, "node is missing")
	}
	sw := bestSwitchForNode(node.Name, nodeInfo, data)
	if sw == nil {
		return framework.NewStatus(framework.Unschedulable, "node has no reachable NetCache switch with enough capacity")
	}
	return nil
}

func (p *Plugin) Score(ctx context.Context, state *framework.CycleState, pod *v1.Pod, nodeName string) (int64, *framework.Status) {
	if !isNetCachePlacementPod(pod) {
		return framework.MaxNodeScore, nil
	}
	data, status := readState(state)
	if !status.IsSuccess() {
		return 0, status
	}
	nodeInfo, err := p.handle.SnapshotSharedLister().NodeInfos().Get(nodeName)
	if err != nil {
		return 0, framework.AsStatus(err)
	}
	node := nodeInfo.Node()
	if node == nil {
		return 0, framework.NewStatus(framework.Error, "node is missing")
	}
	sw := bestSwitchForNode(node.Name, nodeInfo, data)
	if sw == nil {
		return 0, nil
	}

	return clampScore(switchNodeScore(sw, nodeInfo)), nil
}

func (p *Plugin) ScoreExtensions() framework.ScoreExtensions {
	return p
}

func (p *Plugin) NormalizeScore(ctx context.Context, state *framework.CycleState, pod *v1.Pod, scores framework.NodeScoreList) *framework.Status {
	return nil
}

func (p *Plugin) Reserve(ctx context.Context, state *framework.CycleState, pod *v1.Pod, nodeName string) *framework.Status {
	if !isNetCachePlacementPod(pod) {
		return nil
	}
	data, status := readState(state)
	if !status.IsSuccess() {
		return status
	}
	nodeInfo, err := p.handle.SnapshotSharedLister().NodeInfos().Get(nodeName)
	if err != nil {
		return framework.AsStatus(err)
	}
	sw := bestSwitchForNode(nodeName, nodeInfo, data)
	if sw == nil {
		return framework.NewStatus(framework.Error, "selected node has no reachable NetCache switch")
	}
	if sw.freeEntries < data.requiredEntries || sw.freeSlots < data.requiredSlots {
		return framework.NewStatus(framework.Unschedulable, "NetCache capacity changed before reserve")
	}

	newFreeEntries := sw.freeEntries - data.requiredEntries
	newFreeSlots := sw.freeSlots - data.requiredSlots
	statusPatch := map[string]interface{}{
		"status": map[string]interface{}{
			"phase":       phaseFromFree(newFreeEntries, newFreeSlots),
			"usedEntries": sw.usedEntries + data.requiredEntries,
			"freeEntries": newFreeEntries,
			"usedSlots":   sw.usedSlots + data.requiredSlots,
			"freeSlots":   newFreeSlots,
		},
	}
	if err := p.patchSwitchStatus(ctx, sw.name, statusPatch); err != nil {
		return framework.AsStatus(err)
	}

	data.reservedSwitch = sw.name
	data.reservedEntries = data.requiredEntries
	data.reservedSlots = data.requiredSlots
	data.selectedByNode[nodeName] = sw
	state.Write(Name, data)
	return nil
}

func (p *Plugin) Unreserve(ctx context.Context, state *framework.CycleState, pod *v1.Pod, nodeName string) {
	if !isNetCachePlacementPod(pod) {
		return
	}
	data, status := readState(state)
	if !status.IsSuccess() || data.reservedSwitch == "" {
		return
	}
	latest, err := p.getSwitch(ctx, data.reservedSwitch)
	if err != nil {
		return
	}
	capacityEntries := int64Nested(latest, []string{"spec", "capacityEntries"}, 0)
	capacitySlots := int64Nested(latest, []string{"spec", "capacitySlots"}, 0)
	usedEntries := maxInt64(0, int64Nested(latest, []string{"status", "usedEntries"}, 0)-data.reservedEntries)
	usedSlots := maxInt64(0, int64Nested(latest, []string{"status", "usedSlots"}, 0)-data.reservedSlots)
	freeEntries := minInt64(capacityEntries, int64Nested(latest, []string{"status", "freeEntries"}, capacityEntries)+data.reservedEntries)
	freeSlots := minInt64(capacitySlots, int64Nested(latest, []string{"status", "freeSlots"}, capacitySlots)+data.reservedSlots)
	_ = p.patchSwitchStatus(ctx, data.reservedSwitch, map[string]interface{}{
		"status": map[string]interface{}{
			"phase":       phaseFromFree(freeEntries, freeSlots),
			"usedEntries": usedEntries,
			"freeEntries": freeEntries,
			"usedSlots":   usedSlots,
			"freeSlots":   freeSlots,
		},
	})
}

func (p *Plugin) PostBind(ctx context.Context, state *framework.CycleState, pod *v1.Pod, nodeName string) {
	if !isNetCachePlacementPod(pod) {
		return
	}
	data, status := readState(state)
	if !status.IsSuccess() {
		return
	}
	sw := data.selectedByNode[nodeName]
	if sw == nil {
		nodeInfo, err := p.handle.SnapshotSharedLister().NodeInfos().Get(nodeName)
		if err == nil {
			sw = bestSwitchForNode(nodeName, nodeInfo, data)
		}
	}
	if sw == nil {
		return
	}
	annotations := map[string]string{
		annotationSelectedSwitch:   sw.switchName,
		annotationSelectedWorker:   nodeName,
		annotationSelectedHops:     strconv.FormatInt(sw.hops, 10),
		annotationSelectedPathCost: strconv.FormatInt(sw.pathCost, 10),
	}
	if sw.controlURL != "" {
		annotations[annotationSelectedControl] = sw.controlURL
	}
	patch := map[string]interface{}{
		"metadata": map[string]interface{}{
			"annotations": annotations,
		},
	}
	raw, err := json.Marshal(patch)
	if err != nil {
		return
	}
	_, _ = p.handle.ClientSet().CoreV1().Pods(pod.Namespace).Patch(ctx, pod.Name, types.MergePatchType, raw, metav1.PatchOptions{})
}

func (p *Plugin) loadSwitches(ctx context.Context) (map[string]*switchInfo, error) {
	items, err := p.dynamicClient.Resource(switchGVR).List(ctx, metav1.ListOptions{})
	if err != nil {
		if apierrors.IsNotFound(err) {
			return map[string]*switchInfo{}, nil
		}
		return nil, err
	}
	switches := map[string]*switchInfo{}
	for i := range items.Items {
		item := &items.Items[i]
		capacityEntries := int64Nested(item, []string{"spec", "capacityEntries"}, 0)
		capacitySlots := int64Nested(item, []string{"spec", "capacitySlots"}, capacityEntries)
		switchName := stringNested(item, []string{"spec", "switchName"}, item.GetName())
		switches[switchName] = &switchInfo{
			name:            item.GetName(),
			switchName:      switchName,
			attachedWorker:  stringNested(item, []string{"spec", "attachedWorker"}, ""),
			phase:           stringNested(item, []string{"status", "phase"}, "Available"),
			controlURL:      switchControlURL(item),
			deviceReady:     true,
			capacityEntries: capacityEntries,
			capacitySlots:   capacitySlots,
			usedEntries:     int64Nested(item, []string{"status", "usedEntries"}, 0),
			freeEntries:     int64Nested(item, []string{"status", "freeEntries"}, capacityEntries),
			usedSlots:       int64Nested(item, []string{"status", "usedSlots"}, 0),
			freeSlots:       int64Nested(item, []string{"status", "freeSlots"}, capacitySlots),
		}
	}
	return switches, nil
}

func (p *Plugin) loadReachabilities(ctx context.Context) (map[string][]reachabilityCandidate, error) {
	items, err := p.dynamicClient.Resource(reachabilityGVR).List(ctx, metav1.ListOptions{})
	if err != nil {
		if apierrors.IsNotFound(err) {
			return map[string][]reachabilityCandidate{}, nil
		}
		return nil, err
	}
	reachability := map[string][]reachabilityCandidate{}
	for i := range items.Items {
		item := &items.Items[i]
		workerName := stringNested(item, []string{"spec", "workerName"}, item.GetName())
		rawCandidates, found, _ := unstructured.NestedSlice(item.Object, "spec", "candidates")
		if !found {
			continue
		}
		for _, raw := range rawCandidates {
			candidateMap, ok := raw.(map[string]interface{})
			if !ok {
				continue
			}
			switchName := stringFromMap(candidateMap, "switchName", "")
			if switchName == "" {
				continue
			}
			hops := int64FromMap(candidateMap, "hops", 0)
			pathCost := int64FromMap(candidateMap, "pathCost", hops)
			reachability[workerName] = append(reachability[workerName], reachabilityCandidate{
				switchName: switchName,
				hops:       hops,
				pathCost:   pathCost,
			})
		}
	}
	return reachability, nil
}

func fallbackReachability(switches map[string]*switchInfo) map[string][]reachabilityCandidate {
	reachability := map[string][]reachabilityCandidate{}
	for _, sw := range switches {
		if sw.attachedWorker == "" {
			continue
		}
		reachability[sw.attachedWorker] = append(reachability[sw.attachedWorker], reachabilityCandidate{
			switchName: sw.switchName,
			hops:       0,
			pathCost:   0,
		})
	}
	return reachability
}

func (p *Plugin) loadDevices(ctx context.Context) (map[string]*switchInfo, error) {
	items, err := p.dynamicClient.Resource(deviceGVR).List(ctx, metav1.ListOptions{})
	if err != nil {
		if apierrors.IsNotFound(err) {
			return map[string]*switchInfo{}, nil
		}
		return nil, err
	}
	devices := map[string]*switchInfo{}
	for i := range items.Items {
		item := &items.Items[i]
		devices[item.GetName()] = &switchInfo{
			deviceReady:      stringNested(item, []string{"status", "phase"}, "Ready") == "Ready",
			estimatedCPULoad: floatNested(item, []string{"status", "metrics", "estimatedCpuLoad"}, 0),
		}
	}
	return devices, nil
}

func (p *Plugin) getSwitch(ctx context.Context, name string) (*unstructured.Unstructured, error) {
	return p.dynamicClient.Resource(switchGVR).Get(ctx, name, metav1.GetOptions{})
}

func (p *Plugin) patchSwitchStatus(ctx context.Context, name string, patch map[string]interface{}) error {
	raw, err := json.Marshal(patch)
	if err != nil {
		return err
	}
	_, err = p.dynamicClient.Resource(switchGVR).Patch(ctx, name, types.MergePatchType, raw, metav1.PatchOptions{}, "status")
	return err
}

func bestSwitchForNode(nodeName string, nodeInfo *framework.NodeInfo, data *cycleState) *switchInfo {
	candidates := data.candidates[nodeName]
	var best *switchInfo
	bestScore := -1.0
	for _, candidate := range candidates {
		sw := data.switches[candidate.switchName]
		if sw == nil {
			continue
		}
		if sw.phase != "" && sw.phase != "Available" && sw.phase != "Full" {
			continue
		}
		if !sw.deviceReady {
			continue
		}
		if sw.freeEntries < data.requiredEntries || sw.freeSlots < data.requiredSlots {
			continue
		}
		item := *sw
		item.selectedWorker = nodeName
		item.hops = candidate.hops
		item.pathCost = candidate.pathCost
		score := switchNodeScore(&item, nodeInfo)
		if best == nil || score > bestScore || (score == bestScore && tieBreakSwitch(&item, best)) {
			best = &item
			bestScore = score
		}
	}
	return best
}

func switchNodeScore(sw *switchInfo, nodeInfo *framework.NodeInfo) float64 {
	return switchNodeScoreWithWeights(sw, nodeInfo, defaultScoreWeights())
}

func switchNodeScoreWithWeights(sw *switchInfo, nodeInfo *framework.NodeInfo, weights scoreWeights) float64 {
	capacityScore := math.Min(ratio(sw.freeEntries, sw.capacityEntries), ratio(sw.freeSlots, sw.capacitySlots))
	workerScore := 1.0 - workerLoad(nodeInfo)
	deviceScore := 1.0 - deviceLoad(sw)
	hopScore := 1.0 / (1.0 + float64(maxInt64(0, sw.pathCost)))
	return weights.capacity*capacityScore +
		weights.worker*workerScore +
		weights.device*deviceScore +
		weights.hop*hopScore
}

func defaultScoreWeights() scoreWeights {
	return scoreWeights{
		name:     "default",
		capacity: envFloat("NETCACHE_CAPACITY_WEIGHT", defaultCapacityWeight),
		worker:   envFloat("NETCACHE_WORKER_WEIGHT", defaultWorkerWeight),
		device:   envFloat("NETCACHE_DEVICE_WEIGHT", defaultDeviceWeight),
		hop:      envFloat("NETCACHE_HOP_WEIGHT", defaultHopWeight),
	}
}

func tieBreakSwitch(candidate, current *switchInfo) bool {
	if candidate.pathCost != current.pathCost {
		return candidate.pathCost < current.pathCost
	}
	if candidate.freeEntries != current.freeEntries {
		return candidate.freeEntries > current.freeEntries
	}
	return candidate.switchName < current.switchName
}

func switchControlURL(item *unstructured.Unstructured) string {
	if value := stringNested(item, []string{"spec", "controlEndpoint", "url"}, ""); value != "" {
		return value
	}
	host := stringNested(item, []string{"spec", "controlEndpoint", "host"}, "")
	if host == "" {
		host = stringNested(item, []string{"spec", "accessSwitchIp"}, "")
		if host != "" {
			host = splitAddress(host)
		}
	}
	if host == "" {
		return ""
	}
	port := int64Nested(item, []string{"spec", "controlEndpoint", "port"}, 18080)
	return fmt.Sprintf("http://%s:%d", host, port)
}

func readState(state *framework.CycleState) (*cycleState, *framework.Status) {
	raw, err := state.Read(Name)
	if err != nil {
		return nil, framework.AsStatus(err)
	}
	data, ok := raw.(*cycleState)
	if !ok {
		return nil, framework.NewStatus(framework.Error, "invalid NetCache scheduler state")
	}
	return data, nil
}

func isNetCachePlacementPod(pod *v1.Pod) bool {
	return pod.Annotations[annotationEnabled] == "true"
}

func intAnnotation(pod *v1.Pod, key string, fallback int64) (int64, error) {
	raw := pod.Annotations[key]
	if raw == "" {
		return fallback, nil
	}
	value, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || value < 0 {
		return 0, fmt.Errorf("invalid %s annotation: %q", key, raw)
	}
	return value, nil
}

func workerLoad(nodeInfo *framework.NodeInfo) float64 {
	node := nodeInfo.Node()
	if node == nil {
		return 1.0
	}
	allocatable := node.Status.Allocatable
	cpu := resourceRatio(nodeInfo.Requested.MilliCPU, allocatable.Cpu().MilliValue())
	memory := resourceRatio(nodeInfo.Requested.Memory, allocatable.Memory().Value())
	storageQuantity := allocatable[v1.ResourceEphemeralStorage]
	storage := resourceRatio(nodeInfo.Requested.EphemeralStorage, storageQuantity.Value())
	return (cpu + memory + storage) / 3
}

func deviceLoad(sw *switchInfo) float64 {
	return math.Min(1.0, math.Max(0.0, sw.estimatedCPULoad))
}

func ratio(usedOrFree int64, total int64) float64 {
	return resourceRatio(usedOrFree, total)
}

func resourceRatio(usedOrFree int64, total int64) float64 {
	if total <= 0 {
		return 0
	}
	value := float64(usedOrFree) / float64(total)
	return math.Min(1.0, math.Max(0.0, value))
}

func envFloat(name string, fallback float64) float64 {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return fallback
	}
	return value
}

func clampScore(score float64) int64 {
	if score < 0 {
		return 0
	}
	if score > float64(framework.MaxNodeScore) {
		return framework.MaxNodeScore
	}
	return int64(math.Round(score))
}

func phaseFromFree(freeEntries, freeSlots int64) string {
	if freeEntries <= 0 || freeSlots <= 0 {
		return "Full"
	}
	return "Available"
}

func stringNested(obj *unstructured.Unstructured, path []string, fallback string) string {
	value, found, _ := unstructured.NestedString(obj.Object, path...)
	if !found {
		return fallback
	}
	return value
}

func int64Nested(obj *unstructured.Unstructured, path []string, fallback int64) int64 {
	value, found, _ := unstructured.NestedInt64(obj.Object, path...)
	if found {
		return value
	}
	floatValue, found, _ := unstructured.NestedFloat64(obj.Object, path...)
	if found {
		return int64(floatValue)
	}
	return fallback
}

func floatNested(obj *unstructured.Unstructured, path []string, fallback float64) float64 {
	value, found, _ := unstructured.NestedFloat64(obj.Object, path...)
	if found {
		return value
	}
	intValue, found, _ := unstructured.NestedInt64(obj.Object, path...)
	if found {
		return float64(intValue)
	}
	return fallback
}

func stringFromMap(obj map[string]interface{}, key string, fallback string) string {
	value, ok := obj[key].(string)
	if !ok {
		return fallback
	}
	return value
}

func int64FromMap(obj map[string]interface{}, key string, fallback int64) int64 {
	switch value := obj[key].(type) {
	case int64:
		return value
	case int:
		return int64(value)
	case float64:
		return int64(value)
	case string:
		parsed, err := strconv.ParseInt(value, 10, 64)
		if err == nil {
			return parsed
		}
	}
	return fallback
}

func splitAddress(value string) string {
	for i, ch := range value {
		if ch == '/' {
			return value[:i]
		}
	}
	return value
}

func minInt64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

var _ framework.PreFilterPlugin = &Plugin{}
var _ framework.FilterPlugin = &Plugin{}
var _ framework.ScorePlugin = &Plugin{}
var _ framework.ScoreExtensions = &Plugin{}
var _ framework.ReservePlugin = &Plugin{}
var _ framework.PostBindPlugin = &Plugin{}
