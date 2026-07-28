// Flatten NEUT's native event output into a uproot-readable tree.
//
// neutroot2 writes a "neuttree" whose "vectorbranch" holds NeutVect objects.
// uproot can deserialize NeutVect's scalar members from the file's streamers,
// but chokes on the nested TObjArray of NeutPart ("invalid class-tag
// reference"), so the native file cannot be read from Python at all. This
// macro is the NEUT counterpart of GENIE's gntpc: it runs inside the container,
// where libNEUTClass and the NEUT headers are available, and writes a tree of
// plain scalars plus the normalization histograms the normalizer needs.
//
// Run it through setup/neut/nf-neut-flatten, which supplies the .include path
// (without it Cling reports "unknown type name 'NeutVect'").

#include "neutpart.h"
#include "neutvect.h"

#include "TFile.h"
#include "TH1D.h"
#include "TSystem.h"
#include "TTree.h"

void nf_flatten(const char* in_path, const char* out_path) {
  gSystem->Load("libNEUTClass");

  TFile fin(in_path, "READ");
  if (fin.IsZombie()) {
    Error("nf_flatten", "cannot open NEUT output file %s", in_path);
    gSystem->Exit(1);
  }
  TTree* in_tree = (TTree*)fin.Get("neuttree");
  if (!in_tree) {
    Error("nf_flatten", "no 'neuttree' in %s", in_path);
    gSystem->Exit(1);
  }

  NeutVect* nv = new NeutVect();
  in_tree->SetBranchAddress("vectorbranch", &nv);

  TFile fout(out_path, "RECREATE");
  TTree* out_tree = new TTree("nf_neut", "flattened NEUT events");

  Int_t mode = 0;
  Int_t pdgnu = 0;
  Double_t enu_gev = 0.0;
  Double_t totcrs = 0.0;
  out_tree->Branch("mode", &mode, "mode/I");
  out_tree->Branch("pdgnu", &pdgnu, "pdgnu/I");
  out_tree->Branch("enu_gev", &enu_gev, "enu_gev/D");
  out_tree->Branch("totcrs", &totcrs, "totcrs/D");

  const Long64_t n_entries = in_tree->GetEntries();
  for (Long64_t i = 0; i < n_entries; ++i) {
    in_tree->GetEntry(i);
    mode = nv->Mode;
    totcrs = nv->Totcrs;
    // PartInfo(0) is the incoming neutrino; NeutPart four-momenta are in MeV.
    NeutPart* probe = nv->PartInfo(0);
    if (!probe) {
      Error("nf_flatten", "entry %lld has no incoming particle", i);
      gSystem->Exit(1);
    }
    pdgnu = probe->fPID;
    enu_gev = probe->fP.E() / 1000.0;
    out_tree->Fill();
  }

  fout.cd();
  out_tree->Write();

  // neutroot2 stores the flux histogram it sampled and the corresponding event
  // rate (flux x sigma) alongside the events. Their integral ratio is the
  // flux-averaged total cross section in 1e-38 cm^2 per nucleon, which is the
  // only normalization information NEUT emits — carry it across.
  const char* histogram_names[] = {"flux_numu", "evtrt_numu", "fluxhisto", "ratehisto"};
  for (const char* name : histogram_names) {
    TH1D* hist = (TH1D*)fin.Get(name);
    if (hist) {
      fout.cd();
      hist->Write(name);
    }
  }

  fout.Close();
  printf("nf_flatten: wrote %lld events to %s\n", n_entries, out_path);
}
