// Exercise production native registration functions with an identity-aware SDK fake.
#define main included_hotpaths_main
#include "test_hotpaths.cpp"
#undef main
#include <random>
#include <set>
namespace after {
#include "../../native/deadzone_native_preprocess.inc"
}

void clear_faults() { hm::add_fault=0;hm::remove_fault=0;hm::fail_has_at=-1;hm::occupy_next=false; }
void reset_hooks() {
 clear_faults();after::axis_hook::stop();after::key_hook::stop();after::preprocess_hook::stop();
 assert(hm::hooks.empty()); assert(!after::axis_hook::installed&&!after::key_hook::installed);
}
void axis_tests() {
 reset_hooks();py::object receiver{false};std::set<std::wstring> ids;
 for(int i=0;i<10000;++i){
  hm::add_fault=i%2;assert(after::axis_hook::start(receiver));
  assert(ids.insert(after::axis_hook::ID).second);
  assert(hm::hooks.at(after::axis_hook::ID)==after::axis_hook::callback);
  assert(after::axis_hook::start(receiver));assert(hm::hooks.size()==1);
  assert(after::axis_hook::stop());assert(hm::hooks.empty());
 }
 // A generated ID already present at preflight is never claimed or removed.
 clear_faults();hm::occupy_next=true;assert(!after::axis_hook::start(receiver));
 auto occupied=after::axis_hook::ID;assert(hm::hooks.at(occupied)==hm::foreign_callback);
 after::axis_hook::stop();assert(hm::hooks.at(occupied)==hm::foreign_callback);
 hm::hooks.erase(occupied);
 // A failed postflight keeps the possible registration until cleanup.
 clear_faults();hm::fail_has_at=hm::has_count+2;
 try{after::axis_hook::start(receiver);assert(false);}catch(const std::runtime_error&){}
 assert(after::axis_hook::installed);assert(hm::hooks.at(after::axis_hook::ID)==after::axis_hook::callback);
 clear_faults();after::axis_hook::stop();assert(hm::hooks.empty());
 clear_faults();hm::add_fault=2;assert(!after::axis_hook::start(receiver));assert(!after::axis_hook::installed);
 for(int mode : {3,4}){
  reset_hooks();hm::add_fault=mode;try{after::axis_hook::start(receiver);assert(false);}catch(const std::runtime_error&){}
  assert(after::axis_hook::installed);clear_faults();after::axis_hook::stop();assert(hm::hooks.empty());
 }
 for(int mode : {1,2,3}){
  reset_hooks();assert(after::axis_hook::start(receiver));hm::remove_fault=mode;
  assert(!after::axis_hook::stop());assert(after::axis_hook::pending_removal);
  assert(!after::axis_hook::start(receiver));clear_faults();after::axis_hook::stop();
  assert(!after::axis_hook::installed&&!after::axis_hook::pending_removal);assert(hm::hooks.empty());
 }
 reset_hooks();assert(after::axis_hook::start(receiver));auto key=after::axis_hook::ID;
 hm::remove_fault=4;hm::fail_has_at=hm::has_count+1;
 assert(after::axis_hook::stop());assert(!after::axis_hook::installed);
 assert(hm::has_count+1==hm::fail_has_at); // confirmed removal needs no post-check
 assert(hm::hooks.at(key)==hm::foreign_callback);after::axis_hook::stop();assert(hm::hooks.at(key)==hm::foreign_callback);
 hm::hooks.erase(key);clear_faults();
 // SDK's False + presence cannot distinguish this injected exact-ID race.
 hm::add_fault=5;assert(after::axis_hook::start(receiver));key=after::axis_hook::ID;
 assert(hm::hooks.at(key)==hm::foreign_callback);
 hm::hooks.erase(key);clear_faults();after::axis_hook::stop();
 // Foreign replacement after a successful add cannot be identified by SDK.
 assert(after::axis_hook::start(receiver));key=after::axis_hook::ID;
 hm::hooks[key]=hm::foreign_callback;hm::foreign_removed=0;
 after::axis_hook::stop();assert(hm::foreign_removed==1); // documented counterexample
}
void key_tests() {
 reset_hooks();py::object receiver{false};
 hm::add_fault=1;assert(after::key_hook::start(receiver));auto id=after::key_hook::IDENTIFIER;
 assert(hm::hooks.at(id)==after::key_hook::callback);assert(after::key_hook::stop());
 assert(after::key_hook::start(receiver));assert(after::key_hook::IDENTIFIER!=id);
 hm::remove_fault=1;assert(!after::key_hook::stop());assert(!after::key_hook::start(receiver));
 clear_faults();after::key_hook::stop();assert(hm::hooks.empty());
}
void preprocess_tests() {
 reset_hooks();py::object receiver{false};constexpr uintptr_t pi=42;
 hm::add_fault=1;assert(after::preprocess_hook::start(pi,true,false,.1,1,.1,1,false,receiver));
 auto id=after::preprocess_hook::IDENTIFIER;
 assert(hm::hooks.at(id)==after::preprocess_hook::callback);
 hm::remove_fault=1;assert(!after::preprocess_hook::stop());
 assert(!after::preprocess_hook::start(pi,true,false,.1,1,.1,1,false,receiver));
 clear_faults();after::preprocess_hook::stop();assert(hm::hooks.empty());
 assert(after::preprocess_hook::start(pi,true,false,.1,1,.1,1,false,receiver));
 assert(after::preprocess_hook::IDENTIFIER!=id);assert(after::preprocess_hook::stop());
}
void randomized() {
 for(unsigned seed : {7u,20261114u,846203u}){
  reset_hooks();std::mt19937 rng(seed);py::object receiver{false};hm::foreign_removed=0;
  hm::hooks[L"unrelated-foreign"]=hm::foreign_callback;
  std::vector<unsigned> trace;
  for(int i=0;i<10000;++i){
   auto op=rng()%3;unsigned fault;
   if(op==0){fault=rng()%5;hm::add_fault=static_cast<int>(fault);try{after::axis_hook::start(receiver);}catch(const std::runtime_error&){} }
   else {fault=rng()%4;hm::remove_fault=static_cast<int>(fault);after::axis_hook::stop();}
   trace.push_back(op*10+fault);if(trace.size()>30)trace.erase(trace.begin());
   if(hm::hooks.count(L"unrelated-foreign")!=1 || hm::hooks.at(L"unrelated-foreign")!=hm::foreign_callback ||
      hm::foreign_removed!=0 || hm::hooks.size()>2 ||
      (after::axis_hook::pending_removal&&after::axis_hook::enabled)){
    std::cerr<<"seed="<<seed<<" step="<<i<<" recent(op*10+fault)=";
    for(auto event:trace)std::cerr<<event<<",";std::cerr<<"\n";std::abort();
   }
  }
  clear_faults();after::axis_hook::stop();assert(hm::hooks.size()==1);
  hm::hooks.erase(L"unrelated-foreign");
 }
}
int main(){
 static_assert(sizeof(void*)==4,"Win32 required");
 axis_tests();key_tests();preprocess_tests();randomized();
 std::cout<<"PASS: production native hook functions; 10000 IDs; 30000 seeded steps (7, 20261114, 846203)\n";
}
