const VERTEX = `#version 300 es
in vec2 aPosition;
out vec2 vUv;
void main(){ vUv = aPosition * .5 + .5; gl_Position = vec4(aPosition, 0.0, 1.0); }
`;

const FRAGMENT = `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 fragColor;
uniform vec2 uResolution;
uniform float uTime;
uniform float uSeed;
uniform int uSceneA;
uniform int uSceneB;
uniform float uBlend;
uniform float uMotion;
uniform float uComplexity;
uniform vec3 uPalette0;
uniform vec3 uPalette1;
uniform vec3 uPalette2;
uniform vec3 uPalette3;
uniform vec3 uPalette4;

#define PI 3.14159265359
float hash21(vec2 p){ p=fract(p*vec2(123.34,456.21)); p+=dot(p,p+45.32); return fract(p.x*p.y); }
float hash31(vec3 p){ p=fract(p*.1031); p+=dot(p,p.yzx+33.33); return fract((p.x+p.y)*p.z); }
float noise(vec2 p){ vec2 i=floor(p),f=fract(p); f=f*f*(3.-2.*f); return mix(mix(hash21(i),hash21(i+vec2(1,0)),f.x),mix(hash21(i+vec2(0,1)),hash21(i+1.),f.x),f.y); }
float fbm(vec2 p){ float a=.5,v=0.; mat2 m=mat2(1.6,1.2,-1.2,1.6); int octaves=3+int(floor(clamp(uComplexity,0.,1.)*2.)); for(int i=0;i<5;i++){if(i>=octaves)break;v+=a*noise(p);p=m*p+.17;a*=.5;}return v; }
mat2 rot(float a){ float c=cos(a),s=sin(a); return mat2(c,-s,s,c); }
vec3 pal(float x){ x=clamp(x,0.,1.); if(x<.25)return mix(uPalette0,uPalette1,x*4.); if(x<.5)return mix(uPalette1,uPalette2,(x-.25)*4.); if(x<.75)return mix(uPalette2,uPalette3,(x-.5)*4.); return mix(uPalette3,uPalette4,(x-.75)*4.); }
float softLine(float d,float w){ return exp(-abs(d)/max(w,.0001)); }
float invSmooth(float low,float high,float x){ return 1.-smoothstep(low,high,x); }
float quality01(){ return clamp((uComplexity-.5)*2.,0.,1.); }

vec3 infiniteLayers(vec2 p,float t){
  p*=rot(.12*sin(t*.08)); float r=length(p); float a=atan(p.y,p.x); float warp=.09*fbm(vec2(a*1.7+t*.03,r*4.-t*.04));
  float z=1./max(.08,r+warp); float bands=abs(fract(z*1.55+t*.025+.04*sin(a*5.))-0.5); float edge=invSmooth(.01,.2,bands);
  float shade=.12+.88*edge*exp(-r*.55); return vec3(pow(shade,1.35));
}
vec3 organicSheet(vec2 p,float t){
  p*=rot(.22*sin(t*.025)); float a=atan(p.y,p.x),r=length(p); float folds=sin(a*5.+1.5*sin(a*2.-t*.04)+r*9.-t*.05);
  float sheet=.55+.45*cos(r*10.-2.2*folds+fbm(p*2.7+t*.01)*3.); float rim=pow(max(0.,1.-r*.5),2.); float n=.62+.38*fbm(p*4.+vec2(t*.01,-t*.008));
  return vec3(clamp(.12+.85*sheet*rim*n,0.,1.));
}
vec3 topoFlow(vec2 p,float t){
  float h=fbm(p*2.5+vec2(t*.018,-t*.013))+0.26*fbm(p*7.-t*.01); float contours=abs(fract(h*10.)-.5); float lines=invSmooth(.025,.16,contours); float base=.04+.36*h; return vec3(base+lines*.72);
}
vec3 porousSculpture(vec2 p,float t){
  float r=length(p*vec2(.82,1.)); float mass=invSmooth(.25,1.15,r+.25*fbm(p*2.+t*.008)); float cells=fbm(p*6.+vec2(t*.012,-t*.01)); float holes=smoothstep(.67,.82,cells+.13*sin(t*.03+p.x*2.));
  float ridge=softLine(cells-.58,.055); float shade=mass*(.24+.76*(1.-holes))+.32*ridge*mass; return vec3(shade);
}
vec3 reactionDiffusion(vec2 p,float t){
  float a=fbm(p*4.+vec2(t*.015,0.)); float b=fbm(p*4.2+vec2(-t*.011,t*.009)+a*1.8); float v=sin((a-b)*32.+4.*fbm(p*2.)); float membrane=invSmooth(.02,.18,abs(v)); return vec3(.05+.9*membrane*(.55+.45*b));
}
vec3 metaballTunnel(vec2 p,float t){
  float field=0.; float limit=3.+floor(3.*quality01()+.001); for(int i=0;i<6;i++){ float fi=float(i); if(fi>=limit)break; vec2 c=.48*vec2(sin(t*.035*(1.+fi*.09)+fi*1.7),cos(t*.028*(1.+fi*.07)+fi*2.3)); c*=.6+.12*fi; field += .075/(.015+dot(p-c,p-c)); }
  float shell=softLine(field-1.5,.32); float depth=smoothstep(0.,2.7,field); return mix(uPalette0,uPalette2,clamp(.18+shell*.65+depth*.2,0.,1.));
}
vec3 prismBloom(vec2 p,float t){
  float a=atan(p.y,p.x),r=length(p); float petals=.5+.5*cos(a*7.+1.4*sin(a*3.-t*.026)); float membrane=softLine(r-(.38+.34*petals+.08*sin(t*.023+a*2.)),.07);
  float inner=exp(-r*2.8); vec3 c=pal(fract(.62+.18*sin(a*2.+t*.02)+r*.35)); return c*(.15+.9*membrane)+mix(uPalette0,uPalette3,inner)*inner*.65;
}
vec3 spectralFlame(vec2 p,float t){
  p.y+=.35; float rise=(p.y+1.)*.6; float n=fbm(vec2(p.x*3.2,p.y*2.1-t*.12))+0.45*fbm(vec2(p.x*7.-t*.03,p.y*4.-t*.18)); float width=.18+.45*(1.-clamp(rise,0.,1.)); float d=abs(p.x+.25*(n-.5))-width*(.55+.5*n); float flame=invSmooth(-.02,.16,d)*invSmooth(-.35,1.25,p.y);
  float core=invSmooth(-.02,.08,abs(p.x+.12*(n-.5))-.10)*flame; vec3 c=pal(clamp(.2+.72*rise+.18*n,0.,1.)); return c*flame*1.05+vec3(1.)*core*.85;
}
vec3 accretionHorizon(vec2 p,float t){
  p*=rot(.08*sin(t*.02)); float r=length(p),a=atan(p.y,p.x); float diskY=p.y*(2.5+.5*sin(a+t*.02)); float disk=exp(-abs(diskY)/.055)*invSmooth(.25,1.35,r)*smoothstep(.22,.36,r);
  float arc=softLine(r-(.53+.07*sin(a*3.-t*.05)),.035)*(.45+.55*cos(a-t*.04)); float lens=softLine(abs(p.y)-.19/(r+.18),.055)*invSmooth(.35,1.2,r); vec3 hot=mix(vec3(1.,.12,.01),vec3(1.,.9,.2),clamp(1.-r,0.,1.)); vec3 cool=mix(vec3(.03,.2,.8),vec3(.75,.95,1.),lens); vec3 c=hot*(disk+max(0.,arc)) + cool*lens*.55; c*=smoothstep(.24,.32,r); return c;
}
vec3 particleVeil(vec2 p,float t){
  vec3 c=vec3(0.); float layerLimit=1.+floor(2.*quality01()+.001); for(int layer=0;layer<3;layer++){ float L=float(layer); if(L>=layerLimit)break; vec2 q=p*(12.+L*7.); q.x+=sin(p.y*3.+t*.035+L)*2.; q.y+=cos(p.x*2.-t*.028+L)*1.4; vec2 id=floor(q),f=fract(q)-.5; float h=hash21(id+uSeed+L*19.); vec2 o=vec2(hash21(id+3.1),hash21(id+7.7))-.5; float d=length(f-o*.65); float pt=invSmooth(.015,.11,d)*step(.35,h); float veil=.35+.65*fbm(p*2.5+vec2(t*.01,-t*.012)); c+=pal(fract(h+.12*L))*pt*veil*(1.-L*.18); } return c;
}
vec3 chromaticGlass(vec2 p,float t){
  vec3 c=vec3(0.); float minD=10.; float glassLimit=3.+floor(2.*quality01()+.001); for(int i=0;i<5;i++){ float fi=float(i); if(fi>=glassLimit)break; vec2 center=.45*vec2(sin(t*.022+fi*1.37),cos(t*.019+fi*1.91)); float radius=.18+.07*sin(fi*2.4+t*.017); float d=length(p-center)-radius; minD=min(minD,d); float rim=softLine(d,.022); float glow=exp(-max(0.,d)*9.)*step(d,0.); c+=pal(fract(fi*.23+t*.003))*rim*1.2 + pal(fract(.7+fi*.17))*glow*.16; } c+=vec3(.02)*invSmooth(-.18,.08,minD); return c;
}
vec3 volumetricLoom(vec2 p,float t){
  float a=fbm(p*1.8+vec2(t*.012,-t*.007)); float b=fbm((p+vec2(a,-a))*3.1+vec2(-t*.008,t*.011)); float c=fbm((p+vec2(b,a))*5.7-t*.004); float v=.45*a+.35*b+.2*c; float folds=softLine(fract(v*5.+.2*sin(t*.014))-.5,.12); return pal(clamp(v*.88+.08,0.,1.))*(.24+.82*folds);
}
vec3 fieldLines(vec2 p,float t){
  vec2 n1=.33*vec2(sin(t*.022),cos(t*.019)); vec2 n2=.45*vec2(cos(t*.017+2.),sin(t*.024+1.)); vec2 d1=p-n1,d2=p-n2; float ang=atan(d1.y,d1.x)-atan(d2.y,d2.x); float pot=ang*3.5+log(length(d1)+.03)*5.-log(length(d2)+.03)*4.; float lines=softLine(fract(pot/PI)-.5,.055); float fade=smoothstep(.06,.2,min(length(d1),length(d2))); return pal(.55+.35*sin(pot*.13))*lines*fade;
}
vec3 sceneColor(int scene, vec2 p, float t){
  if(scene==0)return infiniteLayers(p,t); if(scene==1)return organicSheet(p,t); if(scene==2)return topoFlow(p,t); if(scene==3)return porousSculpture(p,t); if(scene==4)return reactionDiffusion(p,t); if(scene==5)return metaballTunnel(p,t); if(scene==6)return prismBloom(p,t); if(scene==7)return spectralFlame(p,t); if(scene==8)return accretionHorizon(p,t); if(scene==9)return particleVeil(p,t); if(scene==10)return chromaticGlass(p,t); if(scene==11)return volumetricLoom(p,t); return fieldLines(p,t);
}
void main(){
  vec2 p=(gl_FragCoord.xy*2.-uResolution.xy)/min(uResolution.x,uResolution.y); float t=uTime*uMotion + uSeed*.013;
  vec3 a=sceneColor(uSceneA,p,t); vec3 c=a; if(uSceneA!=uSceneB){vec3 b=sceneColor(uSceneB,p,t);float m=smoothstep(0.,1.,uBlend);c=mix(a,b,m);}
  c=1.-exp(-c*(1.05+.25*uComplexity)); c=pow(max(c,0.),vec3(.92)); fragColor=vec4(c,1.);
}`;

export class GenerativeRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.gl = canvas.getContext('webgl2', { alpha: false, antialias: false, powerPreference: 'high-performance' });
    if (!this.gl) throw new Error('WebGL2 unavailable');
    this.program = createProgram(this.gl, VERTEX, FRAGMENT);
    this.sceneA = 0;
    this.sceneB = 0;
    this.blend = 1;
    this.seed = 1;
    this.motion = 1;
    this.complexity = 1;
    this.palette = [[.01,.01,.01],[.2,.4,.8],[.7,.9,1],[1,.3,.1],[1,.8,.2]];
    this.locations = collectUniforms(this.gl, this.program);
    const vao = this.gl.createVertexArray(); this.gl.bindVertexArray(vao);
    const buffer = this.gl.createBuffer(); this.gl.bindBuffer(this.gl.ARRAY_BUFFER, buffer);
    this.gl.bufferData(this.gl.ARRAY_BUFFER, new Float32Array([-1,-1,3,-1,-1,3]), this.gl.STATIC_DRAW);
    const loc = this.gl.getAttribLocation(this.program, 'aPosition'); this.gl.enableVertexAttribArray(loc); this.gl.vertexAttribPointer(loc,2,this.gl.FLOAT,false,0,0);
  }
  setScenes(a,b,blend){ this.sceneA=a; this.sceneB=b; this.blend=blend; }
  setSeed(seed){ this.seed=seed; }
  setMotion(value){ this.motion=value; }
  setComplexity(value){ this.complexity=value; }
  setPalette(colors){ this.palette=colors; }
  resize(width,height){ if(this.canvas.width!==width||this.canvas.height!==height){this.canvas.width=width;this.canvas.height=height;} this.gl.viewport(0,0,width,height); }
  render(timeSeconds){ const gl=this.gl,l=this.locations; gl.useProgram(this.program); gl.uniform2f(l.uResolution,this.canvas.width,this.canvas.height); gl.uniform1f(l.uTime,timeSeconds); gl.uniform1f(l.uSeed,this.seed); gl.uniform1i(l.uSceneA,this.sceneA); gl.uniform1i(l.uSceneB,this.sceneB); gl.uniform1f(l.uBlend,this.blend); gl.uniform1f(l.uMotion,this.motion); gl.uniform1f(l.uComplexity,this.complexity); for(let i=0;i<5;i++){const c=this.palette[Math.min(i,this.palette.length-1)]||[0,0,0];gl.uniform3f(l['uPalette'+i],c[0],c[1],c[2]);} gl.drawArrays(gl.TRIANGLES,0,3); }
}

function collectUniforms(gl,p){ const out={}; ['uResolution','uTime','uSeed','uSceneA','uSceneB','uBlend','uMotion','uComplexity','uPalette0','uPalette1','uPalette2','uPalette3','uPalette4'].forEach((n)=>out[n]=gl.getUniformLocation(p,n)); return out; }
function createProgram(gl,vsSource,fsSource){ const vs=compile(gl,gl.VERTEX_SHADER,vsSource),fs=compile(gl,gl.FRAGMENT_SHADER,fsSource); const p=gl.createProgram();gl.attachShader(p,vs);gl.attachShader(p,fs);gl.linkProgram(p);if(!gl.getProgramParameter(p,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(p)||'shader link failed');return p; }
function compile(gl,type,source){ const s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(s)||'shader compile failed');return s; }
