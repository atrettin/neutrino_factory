// Single-pass NuWro universe reweighting for neutrino-factory.
//
// A driver over NuWro's own reweighting engines (src/rew/), adapted from its
// reweight_to.cc. reweight_to evaluates one parameter setting per process and
// pays ~12 s of fixed startup each time; this evaluates every universe inside
// one event loop. It also does NOT turn NaN weights into 0 the way reweight_to
// does: non-finite ratios are written as they are, so the normalizer can fail
// loudly on them.
//
// Usage: nf_reweight <events.root> <universes.txt> <weights.root>
//   universes.txt: line 1 holds the parameter names; each further line holds
//   one universe's absolute values, in NuWro units, in the same order.
//   weights.root:  TTree "weights", branch "weights[N]/D" (N universes), one
//   entry per input event, in input order. Each entry is
//   sigma(universe) / sigma(event's own generation parameters).
//
// Built inside the NuWro tree by setup/Dockerfile.nuwro and
// setup/apptainer/nuwro.def, with reweight_to's link line.

#include <cmath>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "../event1.h"
#include "../nucleus.h"
#include "Reweighters.h"
#include "TFile.h"
#include "TTree.h"

void SetupSPP(params &);  // defined in rewRES.cc

static void fail(const std::string &msg) {
  std::cerr << "[nf_reweight] ERROR: " << msg << std::endl;
  std::exit(2);
}

int main(int argc, char *argv[]) {
  if (argc != 4) fail("usage: nf_reweight <events.root> <universes.txt> <weights.root>");

  std::ifstream spec(argv[2]);
  if (!spec) fail(std::string("cannot open ") + argv[2]);
  std::string line;
  std::getline(spec, line);
  std::vector<RewParam *> params;
  {
    std::istringstream names(line);
    std::string name;
    while (names >> name) {
      RewParam &p = rew(name);
      if (p.name == "") fail("parameter '" + name + "' is not reweightable");
      Reweighter &engine = REW(p.engine);
      if (engine.name == "") fail("parameter '" + name + "' has no working engine ('" + p.engine + "')");
      engine.active = true;
      params.push_back(&p);
    }
  }
  if (params.empty()) fail("no parameters on the first line of the universes file");

  std::vector<std::vector<double>> universes;
  while (std::getline(spec, line)) {
    std::istringstream row(line);
    std::vector<double> values;
    double v;
    while (row >> v) values.push_back(v);
    if (values.empty()) continue;
    if (values.size() != params.size()) fail("universe row with the wrong number of values: " + line);
    universes.push_back(values);
  }
  const int n_universes = universes.size();
  if (n_universes == 0) fail("no universes in the universes file");

  TFile input(argv[1]);
  if (input.IsZombie()) fail(std::string("cannot open ") + argv[1]);
  TTree *tree = dynamic_cast<TTree *>(input.Get("treeout"));
  if (!tree) fail("no TTree 'treeout' in the input");
  event *e = new event;
  tree->SetBranchAddress("e", &e);

  TFile output(argv[3], "recreate");
  TTree weights_tree("weights", "Universe weights");
  std::vector<double> weights(n_universes);
  weights_tree.Branch("weights", weights.data(),
                      ("weights[" + std::to_string(n_universes) + "]/D").c_str());

  const bool need_spp = REW("rewRES").active;
  const Long64_t n_events = tree->GetEntries();
  for (Long64_t ie = 0; ie < n_events; ie++) {
    tree->GetEntry(ie);
    // Resets the parameters rew.init knows about to this event's generation
    // values and reconfigures the form factors (see Reweighters::init).
    REW.init(e->par);
    nucleus target(e->par);
    if (ie == 0 && need_spp) SetupSPP(e->par);

    const double nominal = REW.weight(*e, e->par, target);
    for (int u = 0; u < n_universes; u++) {
      // Every active parameter is set for every universe, so no state carries
      // over from the previous one.
      for (size_t j = 0; j < params.size(); j++) params[j]->set(universes[u][j]);
      ff_configure(e->par);
      weights[u] = REW.weight(*e, e->par, target) / nominal;
    }
    weights_tree.Fill();
  }

  output.cd();
  weights_tree.Write();
  output.Close();
  input.Close();
  std::cout << "[nf_reweight] " << n_events << " events x " << n_universes
            << " universes -> " << argv[3] << std::endl;
  return 0;
}
