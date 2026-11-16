#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cctype>
#include <cstdint>
#include <set>
#include <string>
#include <utility>
// Standalone no-Unreal input state machine; clocks supplied for testability.
namespace deadzone {
struct InputState {
 enum Owner:int {Unknown=0,KeyboardMouse=1,Controller=2};
 std::array<double,4> raw{},seen{};
 std::array<bool,2> wake_active{};
 std::set<std::string> held_keys{};
 bool left=false,right=false,priority=true,protect=true;
 double wake=0.05,left_inner=0.10,right_inner=0.10,last_kbm=0.0;
 Owner owner=Unknown;
 void reset() {
   raw.fill(0.0);seen.fill(0.0);wake_active.fill(false);held_keys.clear();
   last_kbm=0;owner=Unknown;
 }
 void configure(bool enable_left,bool enable_right,bool kbm_priority,
                bool need_protect,double wake_threshold,double left_deadzone,
                double right_deadzone) noexcept {
   left=enable_left;right=enable_right;priority=kbm_priority;protect=need_protect;
   wake=std::clamp(wake_threshold,0.01,0.25);
   left_inner=std::clamp(left_deadzone,0.0,1.0);
   right_inner=std::clamp(right_deadzone,0.0,1.0);
   if(!left){raw[0]=raw[1]=0;seen[0]=seen[1]=0;wake_active[0]=false;}
   if(!right){raw[2]=raw[3]=0;seen[2]=seen[3]=0;wake_active[1]=false;}
 }
 void set_owner(Owner next) noexcept {
   owner=next;
 }
 bool can_wake(double now) const noexcept {
   if(!held_keys.empty())return false;
   if(!priority)return true;
   return last_kbm<=0.0||now-last_kbm>=0.10;
 }
 void kbm_activity(double now) noexcept {
   if(now<=0||!std::isfinite(now))return;
   last_kbm=now;set_owner(KeyboardMouse);
 }
 int axis(int index,double value,bool gamepad,double now) noexcept {
   if(!std::isfinite(value)||!std::isfinite(now))return owner;
   if(!gamepad){if(protect&&value!=0.0)kbm_activity(now);return owner;}
   if(index<0||index>3)return owner;
   value=std::clamp(value,-1.0,1.0);
   bool use=index<2?left:right;
   if(use){raw[static_cast<size_t>(index)]=value;
     seen[static_cast<size_t>(index)]=value!=0.0?now:0.0;}
   double threshold=wake;
   if(owner!=Controller&&use&&(owner==KeyboardMouse||last_kbm>0.0)) {
     double inner=index<2?left_inner:right_inner;
     threshold=std::max({threshold,0.11,std::min(1.0,inner+0.015)});
   }
   if(protect&&owner!=Controller&&std::abs(value)>=threshold&&can_wake(now))
     set_owner(Controller);
   return owner;
 }
 int key(const std::string& name,bool gamepad,int event,double now) {
   if(!gamepad){
     if(event==1){held_keys.erase(name);return owner;}
     if(event==2){if(held_keys.count(name))kbm_activity(now);return owner;}
     if((event==0||event==3)&&!name.empty()){
       std::string lower=name;
       std::transform(lower.begin(),lower.end(),lower.begin(),
         [](unsigned char c){return static_cast<char>(std::tolower(c));});
       if(lower.find("mouse")==std::string::npos)held_keys.insert(name);
     }
     kbm_activity(now);return owner;
   }
   if(event!=1&&event!=2&&owner!=Controller&&can_wake(now))set_owner(Controller);
   return owner;
 }
 double sample(int index,double now) noexcept {
   if(index<0||index>3)return 0.0;
   auto i=static_cast<size_t>(index);
   if(raw[i]==0.0)return 0.0;
   if(seen[i]>0&&now-seen[i]<=0.20)return raw[i];
   raw[i]=seen[i]=0.0;return 0.0;
 }
 bool intentional(int stick,double now) noexcept {
   int off=stick*2;double inner=stick==0?left_inner:right_inner;
   if(inner>=1.0){wake_active[stick]=false;return false;}
   double magnitude=std::hypot(sample(off,now),sample(off+1,now));
   double wake_limit=std::max(inner+0.02,wake+0.02);
   double rest_limit=std::max(inner,std::min(wake,0.07)*0.8);
   bool active=wake_active[stick];
   active=active?(magnitude>rest_limit):(magnitude>=wake_limit);
   wake_active[stick]=active;return active;
 }
 std::pair<bool,bool> guard(double now) noexcept {
   bool isolate=priority&&owner!=Controller;
   bool live_l=isolate&&left?intentional(0,now):false;
   bool live_r=isolate&&right?intentional(1,now):false;
   return {left&&((isolate&&!live_l)||left_inner>=1.0),
           right&&((isolate&&!live_r)||right_inner>=1.0)};
 }
 bool frame_allowed(bool engine_gamepad,double now) noexcept {
   bool okay=engine_gamepad&&owner!=KeyboardMouse&&held_keys.empty()&&
        (last_kbm<=0.0||now-last_kbm>=0.035);
   return okay;
 }
};
}
